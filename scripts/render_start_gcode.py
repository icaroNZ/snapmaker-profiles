#!/usr/bin/env python3
"""Render Snapmaker Orca custom G-code templates offline (subset of Orca's PlaceholderParser).

Used by validate_profiles.py (rule SM2-GC-PURGE) and its tests to check the SM2 start G-code for
concrete first-layer footprints, and usable on its own:

    python3 -I scripts/render_start_gcode.py "Snapmaker A250 (0.4 nozzle)" --footprint 90,100,140,150

The semantics follow OrcaSlicer src/libslic3r/PlaceholderParser.cpp (a250t/main):
* text outside braces is copied verbatim; ``[name]`` is a legacy expansion, and any other ``[`` in
  text is a parse error in Orca (``lit('[') > legacy_variable_expansion > ']'``), so it is one here too;
* ``{if c}..{elsif c}..{else}..{endif}`` text blocks; skipped branches are parsed but not evaluated;
* ``{stmt; stmt}`` macros with ``local name = expr`` / ``name = expr`` / expression statements; a
  ``local`` whose right-hand side starts with ``(`` is a vector initialiser in Orca (and a parse error
  when more follows), so it is rejected here;
* operators ``?: || && == != <> < > <= >= + - * / % ! not and or``; int/int division truncates, int
  stays int through min/max, out-of-range vector indices read element 0, doubles print like
  ``std::ostream`` (%g, 6 significant digits); ``digits(v, w[, d])``, ``int()``, ``round()``.
Features the SM2 templates do not use (regex, one_of, interpolate_table, repeat, global, ...) raise
RenderError so a template that needs them is noticed instead of silently mis-rendered.
"""

import argparse
import glob
import json
import math
import os
import re
import sys


class RenderError(Exception):
    pass


KEYWORDS = {"and", "digits", "zdigits", "empty", "if", "int", "is_nil", "local", "else", "elsif", "endif", "false",
            "global", "interpolate_table", "min", "max", "random", "repeat", "round", "not", "one_of", "or", "size",
            "true"}
_TOKEN_RE = re.compile(r"""\s*(?:
    (?P<num>\d+\.\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|\d+[eE][-+]?\d+|\d+)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>\|\||&&|==|!=|<>|<=|>=|[-+*/%<>!?:(),;\[\]=])
)""", re.X)


def _tokenize(src):
    toks, pos = [], 0
    while pos < len(src):
        if src[pos:].strip() == "":
            break
        m = _TOKEN_RE.match(src, pos)
        if not m or m.end() == pos:
            raise RenderError("cannot tokenize macro at %r" % src[pos:pos + 20])
        pos = m.end()
        if m.group("num"):
            t = m.group("num")
            toks.append(("num", float(t) if any(c in t for c in ".eE") else int(t)))
        elif m.group("str"):
            toks.append(("str", bytes(m.group("str")[1:-1], "utf-8").decode("unicode_escape")))
        elif m.group("id"):
            toks.append(("id", m.group("id")))
        else:
            toks.append(("op", m.group("op")))
    return toks


class _Expr:
    """Recursive descent over one macro's tokens; builds a tuple AST."""

    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def take(self, kind=None, val=None):
        tok = self.peek()
        if (kind and tok[0] != kind) or (val is not None and tok[1] != val):
            raise RenderError("expected %s %r, got %r" % (kind, val, tok))
        self.i += 1
        return tok

    def at(self, kind, val=None):
        tok = self.peek()
        return tok[0] == kind and (val is None or tok[1] == val)

    def at_kw(self, *names):
        return self.peek()[0] == "id" and self.peek()[1] in names

    def statements(self):
        out = []
        while self.i < len(self.t):
            if self.at("op", ";"):
                self.i += 1
                continue
            out.append(self.statement())
            if self.i < len(self.t):
                self.take("op", ";")
        return out

    def statement(self):
        if self.at_kw("global", "repeat"):
            raise RenderError("%r is not supported by this renderer" % self.peek()[1])
        if self.at_kw("local"):
            self.i += 1
            name = self.identifier()
            self.take("op", "=")
            if self.at("op", "("):
                raise RenderError("local %s = (...): Orca parses a leading '(' as a vector initializer" % name)
            return ("local", name, self.conditional())
        if self.peek()[0] == "id" and self.peek(1) == ("op", "=") and self.peek()[1] not in KEYWORDS:
            name = self.identifier()
            self.take("op", "=")
            return ("assign", name, self.conditional())
        return ("expr", self.conditional())

    def identifier(self):
        kind, val = self.take("id")
        if val in KEYWORDS:
            raise RenderError("keyword %r used as identifier" % val)
        return val

    def conditional(self):
        cond = self.logical_or()
        if self.at("op", "?"):
            self.i += 1
            a = self.conditional()
            self.take("op", ":")
            b = self.conditional()
            return ("?", cond, a, b)
        return cond

    def _binary(self, sub, ops):
        node = sub()
        while True:
            tok = self.peek()
            op = tok[1] if tok[0] == "op" and tok[1] in ops else None
            if op is None and tok[0] == "id" and tok[1] in ops:
                op = tok[1]
            if op is None:
                return node
            self.i += 1
            node = ({"or": "||", "and": "&&", "<>": "!="}.get(op, op), node, sub())

    def logical_or(self):
        return self._binary(self.logical_and, ("||", "or"))

    def logical_and(self):
        return self._binary(self.equality, ("&&", "and"))

    def equality(self):
        return self._binary(self.relational, ("==", "!=", "<>"))

    def relational(self):
        return self._binary(self.additive, ("<=", ">=", "<", ">"))

    def additive(self):
        return self._binary(self.multiplicative, ("+", "-"))

    def multiplicative(self):
        return self._binary(self.unary, ("*", "/", "%"))

    def unary(self):
        kind, val = self.peek()
        if kind == "num":
            self.i += 1
            return ("lit", val)
        if kind == "str":
            self.i += 1
            return ("lit", val)
        if kind == "op" and val == "(":
            self.i += 1
            node = self.conditional()
            self.take("op", ")")
            return node
        if kind == "op" and val in ("-", "+", "!"):
            self.i += 1
            return ({"-": "neg", "+": "pos", "!": "not"}[val], self.unary())
        if kind == "id":
            if val in ("true", "false"):
                self.i += 1
                return ("lit", val == "true")
            if val == "not":
                self.i += 1
                return ("not", self.unary())
            if val in ("min", "max", "digits", "zdigits", "int", "round"):
                self.i += 1
                self.take("op", "(")
                args = [self.conditional()]
                while self.at("op", ","):
                    self.i += 1
                    args.append(self.conditional())
                self.take("op", ")")
                want = {"min": (2,), "max": (2,), "int": (1,), "round": (1,), "digits": (2, 3), "zdigits": (2, 3)}
                if len(args) not in want[val]:
                    raise RenderError("%s() takes %s arguments" % (val, want[val]))
                return ("call", val, args)
            if val in KEYWORDS:
                raise RenderError("keyword %r is not supported by this renderer" % val)
            self.i += 1
            if self.at("op", "["):
                self.i += 1
                idx = self.conditional()
                self.take("op", "]")
                return ("index", val, idx)
            return ("var", val)
        raise RenderError("unexpected token %r" % (self.peek(),))


def _split_macros(text):
    """Text -> list of ('text', s) / ('legacy', name, index_name|None) / ('macro', src)."""
    out, i, n = [], 0, len(text)
    while i < n:
        j = min([k for k in (text.find("{", i), text.find("[", i)) if k >= 0] or [n])
        if j > i:
            out.append(("text", text[i:j]))
        if j >= n:
            break
        if text[j] == "[":
            m = re.match(r"\[([A-Za-z_]\w*)(?:\[([A-Za-z_]\w*)\])?\]", text[j:])
            if not m or m.group(1) in KEYWORDS:
                raise RenderError("'[' in text must start a legacy [variable] (Orca parse error): %r" % text[j:j + 30])
            out.append(("legacy", m.group(1), m.group(2)))
            i = j + m.end()
            continue
        k, in_str = j + 1, False
        while k < n and (in_str or text[k] != "}"):
            if text[k] == '"' and text[k - 1] != "\\":
                in_str = not in_str
            k += 1
        if k >= n:
            raise RenderError("unterminated '{' at %r" % text[j:j + 30])
        out.append(("macro", text[j + 1:k]))
        i = k + 1
    return out


def parse_template(text):
    """Parse into a node list: ('text', s), ('legacy', name, idx), ('stmts', [..]), ('if', [(cond, nodes)], else)."""
    root = []
    stack = [("root", root, None)]  # (kind, current node list, if-node)
    for part in _split_macros(str(text)):
        cur = stack[-1][1]
        if part[0] != "macro":
            cur.append(part)
            continue
        toks = _tokenize(part[1])
        head = toks[0] if toks else (None, None)
        if head == ("id", "if"):
            p = _Expr(toks[1:])
            cond = p.conditional()
            if p.i != len(p.t):
                raise RenderError("macro-style {if ... then ...} is not supported: {%s}" % part[1])
            node = ("if", [(cond, [])], [])
            cur.append(node)
            stack.append(("if", node[1][0][1], node))
        elif head == ("id", "elsif"):
            if stack[-1][0] != "if":
                raise RenderError("{elsif} without {if}")
            p = _Expr(toks[1:])
            cond = p.conditional()
            node = stack.pop()[2]
            node[1].append((cond, []))
            stack.append(("if", node[1][-1][1], node))
        elif head == ("id", "else"):
            if stack[-1][0] != "if" or len(toks) != 1:
                raise RenderError("bad {else}")
            node = stack.pop()[2]
            stack.append(("else", node[2], node))
        elif head == ("id", "endif"):
            if stack[-1][0] not in ("if", "else") or len(toks) != 1:
                raise RenderError("{endif} without {if}")
            stack.pop()
        else:
            p = _Expr(toks)
            cur.append(("stmts", p.statements()))
    if len(stack) != 1:
        raise RenderError("{if} without {endif}")
    return root


# ---- evaluation --------------------------------------------------------------------------------------------------

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num(v, what):
    if not _is_num(v):
        raise RenderError("%s: not a number: %r" % (what, v))
    return v


def _fmt(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return "%g" % v
    return str(v)


def _cround(v):  # std::round: halves away from zero; ints unchanged
    return v if isinstance(v, int) else int(math.copysign(math.floor(abs(v) + 0.5), v))


def _cdiv(a, b):  # C++ integer division truncates toward zero
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


class _Ctx:
    def __init__(self, variables, current_extruder):
        self.vars, self.local, self.ext = variables, {}, current_extruder

    def lookup(self, name):
        if name in self.local:
            return self.local[name]
        if name in self.vars:
            return self.vars[name]
        raise RenderError("Variable does not exist: %s" % name)

    def ev(self, node):
        op = node[0]
        if op == "lit":
            return node[1]
        if op == "var":
            v = self.lookup(node[1])
            if isinstance(v, list):
                raise RenderError("Referencing a vector variable when scalar is expected: %s" % node[1])
            return v
        if op == "index":
            v = self.lookup(node[1])
            if not isinstance(v, list):
                raise RenderError("Trying to index a scalar variable: %s" % node[1])
            if not v:
                raise RenderError("Indexing an empty vector variable: %s" % node[1])
            idx = self.ev(node[2])
            if not isinstance(idx, int) or isinstance(idx, bool):
                raise RenderError("vector index of %s is not an int: %r" % (node[1], idx))
            return v[idx] if 0 <= idx < len(v) else v[0]
        if op == "?":
            c = self.ev(node[1])
            if not isinstance(c, bool):
                raise RenderError("Not a boolean expression (ternary)")
            return self.ev(node[2] if c else node[3])
        if op in ("neg", "pos"):
            v = _num(self.ev(node[1]), op)
            return -v if op == "neg" else v
        if op == "not":
            v = self.ev(node[1])
            if not isinstance(v, bool):
                raise RenderError("Not a boolean expression (!)")
            return not v
        if op == "call":
            return self.call(node[1], [self.ev(a) for a in node[2]])
        a, b = self.ev(node[1]), self.ev(node[2])
        if op in ("||", "&&"):
            if not (isinstance(a, bool) and isinstance(b, bool)):
                raise RenderError("%s needs booleans" % op)
            return (a or b) if op == "||" else (a and b)
        if op in ("==", "!="):
            if _is_num(a) and _is_num(b):  # Orca compare_op: doubles equal within 1e-8
                eq = abs(float(a) - float(b)) < 1e-8 if (isinstance(a, float) or isinstance(b, float)) else a == b
            elif type(a) == type(b):
                eq = a == b
            else:
                raise RenderError("cannot compare %r with %r" % (a, b))
            return eq if op == "==" else not eq
        if op in ("<", ">", "<=", ">="):
            if not ((_is_num(a) and _is_num(b)) or (isinstance(a, str) and isinstance(b, str))):
                raise RenderError("cannot compare %r with %r" % (a, b))
            return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[op]
        if op == "+" and (isinstance(a, str) or isinstance(b, str)):
            return _fmt(a) + _fmt(b)
        a, b = _num(a, op), _num(b, op)
        both_int = isinstance(a, int) and isinstance(b, int)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op in ("/", "%"):
            if b == 0:
                raise RenderError("Division by zero")
            if op == "/":
                return _cdiv(a, b) if both_int else float(a) / float(b)
            return (a - _cdiv(a, b) * b) if both_int else math.fmod(a, b)
        raise RenderError("unknown operator %r" % op)

    @staticmethod
    def call(fn, args):
        if fn in ("min", "max"):
            a, b = _num(args[0], fn), _num(args[1], fn)
            r = (min if fn == "min" else max)(float(a), float(b))
            return r if (isinstance(a, float) or isinstance(b, float)) else int(r)
        if fn == "int":
            return int(_num(args[0], fn))
        if fn == "round":
            return _cround(_num(args[0], fn))
        v, w = _num(args[0], fn), args[1]
        if not isinstance(w, int) or isinstance(w, bool):
            raise RenderError("digits: second parameter must be integer")
        flag = "0" if fn == "zdigits" else ""
        if len(args) == 3:
            if not isinstance(args[2], int) or isinstance(args[2], bool):
                raise RenderError("digits: third parameter must be integer")
            return ("%" + flag + "*.*f") % (max(0, min(w, 64)), max(0, min(args[2], 64)), v)
        return ("%" + flag + "*d") % (max(0, min(w, 64)), _cround(v))

    def run(self, nodes, out):
        for node in nodes:
            kind = node[0]
            if kind == "text":
                out.append(node[1])
            elif kind == "legacy":
                name, idx = node[1], None
                base, _, num = name.rpartition("_")
                if name not in self.local and name not in self.vars and base and num.isdigit():
                    name, idx = base, int(num)  # Orca legacy "[vector_N]" indexing
                v = self.lookup(name)
                if isinstance(v, list):
                    if idx is None:
                        idx = self.ext if node[2] is None else self.lookup(node[2])
                    v = v[idx] if 0 <= idx < len(v) else v[0]
                out.append(_fmt(v))
            elif kind == "stmts":
                for st in node[1]:
                    if st[0] == "local":
                        if st[1] in self.vars:
                            raise RenderError("Symbol is already defined in read-only system dictionary: %s" % st[1])
                        self.local[st[1]] = self.ev(st[2])
                    elif st[0] == "assign":
                        if st[1] not in self.local:
                            raise RenderError("Cannot modify a read-only variable: %s" % st[1])
                        new = self.ev(st[2])
                        old = self.local[st[1]]
                        if type(new) != type(old) and not (_is_num(new) and _is_num(old)):
                            raise RenderError("assignment changes the type of %s" % st[1])
                        self.local[st[1]] = float(new) if isinstance(old, float) else new
                    else:
                        out.append(_fmt(self.ev(st[1])))
            elif kind == "if":
                for cond, body in node[1]:
                    c = self.ev(cond)
                    if not isinstance(c, bool):
                        raise RenderError("Not a boolean expression ({if})")
                    if c:
                        self.run(body, out)
                        break
                else:
                    self.run(node[2], out)


_PARSE_CACHE = {}


def render(text, variables, current_extruder=0):
    """Render ``text`` with ``variables`` (name -> int/float/bool/str or list of them). Raises RenderError."""
    key = str(text)
    if key not in _PARSE_CACHE:
        _PARSE_CACHE[key] = parse_template(key)
    ctx = _Ctx(variables, current_extruder)
    out = []
    ctx.run(_PARSE_CACHE[key], out)
    return "".join(out), ctx.local


# ---- SM2 start G-code helpers -----------------------------------------------------------------------------------

def _points(values):
    pts = []
    for p in values or []:
        x, _, y = str(p).partition("x")
        pts.append((float(x), float(y)))
    return pts


def bed_geometry(printer_cfg):
    """(bed bbox (x0, y0, x1, y1), list of exclude-area bboxes) from a resolved machine preset."""
    pts = _points(printer_cfg.get("printable_area")) or [(0.0, 0.0)]
    bed = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    ex = _points(printer_cfg.get("bed_exclude_area"))
    excl = []
    for k in range(0, len(ex) - len(ex) % 4, 4):  # Orca stores each exclude area as 4 corner points
        quad = ex[k:k + 4]
        excl.append((min(p[0] for p in quad), min(p[1] for p in quad), max(p[0] for p in quad), max(p[1] for p in quad)))
    return bed, excl


def usable_bed(printer_cfg):
    """Bed bbox with back-strip exclude areas (all SM2 kits and Dual) cut off."""
    bed, excl = bed_geometry(printer_cfg)
    x0, y0, x1, y1 = bed
    for ex in excl:
        if ex[0] <= x0 and ex[2] >= x1 and ex[3] >= y1:
            y1 = min(y1, ex[1])
    return (x0, y0, x1, y1)


def _floats(value, default):
    out = []
    for v in (value if isinstance(value, list) else [value]):
        try:
            out.append(float(str(v).strip().rstrip("%")))
        except (TypeError, ValueError):
            pass
    return out or [default]


def sample_variables(printer_cfg, footprint, initial_extruder=0, mvs=15.0, filament_diameter=1.75,
                     bed_temp=60, nozzle_temp=210, ooze_prevention=False):
    """Placeholder values for the SM2 start G-code: geometry from the machine preset, filament values sampled.

    footprint = (x0, y0, x1, y1) of the first layer in bed coordinates (Orca first_layer_print_min/max)."""
    bed, _ = bed_geometry(printer_cfg)
    nozzles = _floats(printer_cfg.get("nozzle_diameter"), 0.4)
    n = max(2, len(nozzles))
    mvs_list = mvs if isinstance(mvs, list) else [mvs] * n
    fx0, fy0, fx1, fy1 = footprint
    return {
        "printer_preset": "sample printer", "print_preset": "sample process", "plate_name": "sample plate",
        "initial_extruder": initial_extruder, "current_extruder": initial_extruder,
        "first_layer_bed_temperature": [bed_temp] * n, "bed_temperature": [bed_temp] * n,
        "filament_type": ["PLA"] * n, "nozzle_temperature_range_high": [240] * n,
        "nozzle_temperature_initial_layer": [nozzle_temp] * n, "nozzle_temperature": [nozzle_temp] * n,
        "fan_max_speed": [100.0] * n, "retraction_length": _floats(printer_cfg.get("retraction_length"), 0.8) * n,
        "ooze_prevention": ooze_prevention, "is_extruder_used": [True] * n, "standby_temperature_delta": -60,
        "nozzle_diameter": nozzles, "filament_diameter": [filament_diameter] * n,
        "filament_max_volumetric_speed": [float(v) for v in mvs_list],
        "first_layer_print_min": [float(fx0), float(fy0)], "first_layer_print_max": [float(fx1), float(fy1)],
        "first_layer_print_size": [float(fx1 - fx0), float(fy1 - fy0)],
        "print_bed_min": [bed[0], bed[1]], "print_bed_max": [bed[2], bed[3]],
        "print_bed_size": [bed[2] - bed[0], bed[3] - bed[1]],
        "max_layer_z": 20.0, "printable_height": float(_floats(printer_cfg.get("printable_height"), 230.0)[0]),
    }


_WORD_RE = re.compile(r"([A-Z])(-?\d*\.?\d+)")


def gcode_moves(gcode):
    """G0/G1 moves of rendered G-code: list of dicts with absolute x/y (carried over), relative e, f, line."""
    moves, x, y = [], None, None
    for line in gcode.split("\n"):
        code = line.split(";", 1)[0].strip()
        m = re.match(r"G([01])\b(.*)", code)
        if not m:
            continue
        words = {k: float(v) for k, v in _WORD_RE.findall(m.group(2))}
        if "{" in code or "[" in code:
            raise RenderError("unrendered placeholder in %r" % code)
        nx, ny = words.get("X", x), words.get("Y", y)
        moves.append({"g": int(m.group(1)), "x0": x, "y0": y, "x": nx, "y": ny, "e": words.get("E"),
                      "f": words.get("F"), "xy": "X" in words or "Y" in words, "line": code})
        x, y = nx, ny
    return moves


def load_machine_presets(root):
    by_name = {}
    for path in glob.glob(os.path.join(root, "Snapmaker", "machine", "*.json")):
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("name"):
            by_name[data["name"]] = data
    return by_name


def resolve_machine(by_name, name):
    chain, cfg, cur = [], {}, name
    while cur:
        if cur not in by_name or cur in chain:
            raise RenderError("cannot resolve machine preset %r" % cur)
        chain.append(cur)
        cur = by_name[cur].get("inherits")
    for n in reversed(chain):
        cfg.update(by_name[n])
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("printer", help='machine preset name, e.g. "Snapmaker A250 (0.4 nozzle)"')
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--footprint", default="90,100,140,150", help="first layer x0,y0,x1,y1 in mm")
    ap.add_argument("--extruder", type=int, default=0, help="initial_extruder")
    ap.add_argument("--mvs", type=float, default=15.0, help="filament_max_volumetric_speed (mm^3/s)")
    ap.add_argument("--key", default="machine_start_gcode")
    args = ap.parse_args(argv)
    cfg = resolve_machine(load_machine_presets(args.root), args.printer)
    fp = tuple(float(v) for v in args.footprint.split(","))
    text, local = render(cfg.get(args.key, ""), sample_variables(cfg, fp, args.extruder, args.mvs), args.extruder)
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
