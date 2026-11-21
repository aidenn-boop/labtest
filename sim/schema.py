"""Minimal draft-07 validator for sim/data/mechfight.schema.json (subset used by the schema)."""
from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


class Validator:
    def __init__(self, path=None):
        with open(path or os.path.join(HERE, "data", "mechfight.schema.json")) as f:
            self.root = json.load(f)

    def errors(self, value, name):
        out = []
        self._check(value, {"$ref": "#/definitions/" + name}, "$", out)
        return out

    @staticmethod
    def _type_ok(v, t):
        return {"object": isinstance(v, dict), "array": isinstance(v, list), "string": isinstance(v, str), "boolean": isinstance(v, bool),
                "null": v is None, "number": isinstance(v, (int, float)) and not isinstance(v, bool),
                "integer": isinstance(v, int) and not isinstance(v, bool)}.get(t, True)

    def _sub(self, v, s, at):
        o = []
        self._check(v, s, at, o)
        return o

    def _check(self, v, s, at, out):
        if len(out) > 8:
            return
        if "$ref" in s:
            return self._check(v, self.root["definitions"][s["$ref"].split("/")[-1]], at, out)
        alts = s.get("anyOf") or s.get("oneOf")
        if alts:
            if not any(not self._sub(v, a, at) for a in alts):
                errs = [self._sub(v, a, at) for a in alts]
                out.append(f"{at}: matches none of the allowed shapes ({min(errs, key=len)[:2]})")
            return
        t = s.get("type")
        if t is not None:
            ts = t if isinstance(t, list) else [t]
            if not any(self._type_ok(v, x) for x in ts):
                out.append(f"{at}: expected {'/'.join(ts)}, got {type(v).__name__}")
                return
        if "const" in s and v != s["const"]:
            out.append(f"{at}: expected {json.dumps(s['const'])}")
        if "enum" in s and v not in s["enum"]:
            out.append(f"{at}: expected one of {s['enum']}")
        if isinstance(v, str) and "pattern" in s and not re.search(s["pattern"], v):
            out.append(f"{at}: does not match {s['pattern']}")
        if isinstance(v, dict):
            props = s.get("properties", {})
            for r in s.get("required", []):
                if r not in v:
                    out.append(f"{at}: missing {r}")
            for k, x in v.items():
                if k in props:
                    self._check(x, props[k], f"{at}.{k}", out)
                elif s.get("additionalProperties") is False:
                    out.append(f"{at}: unexpected {k}")
                elif isinstance(s.get("additionalProperties"), dict):
                    self._check(x, s["additionalProperties"], f"{at}.{k}", out)
        if isinstance(v, list):
            if "minItems" in s and len(v) < s["minItems"]:
                out.append(f"{at}: fewer than {s['minItems']} items")
            if "maxItems" in s and len(v) > s["maxItems"]:
                out.append(f"{at}: more than {s['maxItems']} items")
            it = s.get("items")
            if isinstance(it, dict):
                for i, x in enumerate(v):
                    self._check(x, it, f"{at}[{i}]", out)
            elif isinstance(it, list):
                for i, (x, si) in enumerate(zip(v, it)):
                    self._check(x, si, f"{at}[{i}]", out)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if "minimum" in s and v < s["minimum"]:
                out.append(f"{at}: below minimum {s['minimum']}")
            if "maximum" in s and v > s["maximum"]:
                out.append(f"{at}: above maximum {s['maximum']}")
