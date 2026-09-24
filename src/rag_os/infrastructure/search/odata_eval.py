"""A small evaluator for the OData $filter subset RAG-OS generates.

Used by the local/in-memory search backends so that development and tests enforce *exactly the same
filter string* that Azure AI Search receives. Supported:

    expr      := or ( 'or' or )*            primary := '(' expr ')' | 'not' primary | cmp | any | search.in | bool
    cmp       := operand op literal         op      := eq ne lt le gt ge
    any       := field '/any(' var ':' expr ')'
    search.in := 'search.in(' operand ',' 'string' [ ',' 'delims' ] ')'
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

Predicate = Callable[[dict[str, Any]], bool]

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+(?:\.\d+)?)|(?P<punc>[(),:/])|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))"
)
_OPS = {"eq", "ne", "lt", "le", "gt", "ge"}


class ODataSyntaxError(ValueError):
    pass


def _tokenize(s: str) -> list[tuple[str, str]]:
    pos = 0
    out: list[tuple[str, str]] = []
    s = s.strip()
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m or m.end() == pos:
            raise ODataSyntaxError(f"unexpected input at {pos}: {s[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        assert kind is not None
        out.append((kind, m.group(kind)))
    return out


class _Parser:
    def __init__(self, text: str) -> None:
        self.toks = _tokenize(text)
        self.i = 0

    def peek(self, k: int = 0) -> tuple[str, str] | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        t = self.peek()
        if t is None:
            raise ODataSyntaxError("unexpected end")
        if value is not None and t[1] != value:
            raise ODataSyntaxError(f"expected {value!r} got {t[1]!r}")
        self.i += 1
        return t

    def parse(self) -> Predicate:
        p = self.expr({})
        if self.peek() is not None:
            raise ODataSyntaxError(f"trailing tokens: {self.toks[self.i:]}")
        return p

    def expr(self, scope: dict[str, int]) -> Predicate:
        left = self.and_expr(scope)
        while self.peek() and self.peek()[1] == "or":  # type: ignore[index]
            self.take("or")
            right = self.and_expr(scope)
            left = (lambda a, b: lambda d: a(d) or b(d))(left, right)
        return left

    def and_expr(self, scope: dict[str, int]) -> Predicate:
        left = self.primary(scope)
        while self.peek() and self.peek()[1] == "and":  # type: ignore[index]
            self.take("and")
            right = self.primary(scope)
            left = (lambda a, b: lambda d: a(d) and b(d))(left, right)
        return left

    def primary(self, scope: dict[str, int]) -> Predicate:
        t = self.peek()
        if t is None:
            raise ODataSyntaxError("unexpected end")
        if t[1] == "(":
            self.take("(")
            e = self.expr(scope)
            self.take(")")
            return e
        if t[1] == "not":
            self.take("not")
            inner = self.primary(scope)
            return lambda d: not inner(d)
        if t[1] in ("true", "false"):
            self.take()
            val = t[1] == "true"
            return lambda d: val
        if t[1] == "search.in":
            return self.search_in(scope)
        if t[0] == "word":
            name = self.take()[1]
            nxt = self.peek()
            if nxt and nxt[1] == "/":
                self.take("/")
                self.take("any")
                self.take("(")
                var = self.take()[1]
                self.take(":")
                inner = self.expr({**scope, var: 1})
                self.take(")")
                field = name

                def any_pred(d: dict[str, Any], field: str = field, var: str = var, inner: Predicate = inner) -> bool:
                    values = d.get(field) or []
                    return any(inner({**d, var: v}) for v in values)

                return any_pred
            op = self.take()[1]
            if op not in _OPS:
                raise ODataSyntaxError(f"unknown operator {op!r}")
            lit = self.literal()
            return _compare(name, op, lit)
        raise ODataSyntaxError(f"unexpected token {t[1]!r}")

    def search_in(self, scope: dict[str, int]) -> Predicate:
        self.take("search.in")
        self.take("(")
        name = self.take()[1]
        self.take(",")
        values = self.literal()
        delims = ", "
        if self.peek() and self.peek()[1] == ",":  # type: ignore[index]
            self.take(",")
            delims = str(self.literal())
        self.take(")")
        if not isinstance(values, str):
            raise ODataSyntaxError("search.in expects a string list")
        pattern = "[" + re.escape(delims) + "]"
        allowed = {v for v in re.split(pattern, values) if v != ""}
        return lambda d: d.get(name) in allowed

    def literal(self) -> Any:
        kind, val = self.take()
        if kind == "str":
            return val[1:-1].replace("''", "'")
        if kind == "num":
            return float(val) if "." in val else int(val)
        if val in ("true", "false"):
            return val == "true"
        if val == "null":
            return None
        raise ODataSyntaxError(f"expected literal, got {val!r}")


def _compare(name: str, op: str, lit: Any) -> Predicate:
    def pred(d: dict[str, Any]) -> bool:
        v = d.get(name)
        if op == "eq":
            return bool(v == lit)
        if op == "ne":
            return bool(v != lit)
        if v is None or lit is None:
            return False  # OData: comparisons with null are false
        try:
            if op == "lt":
                return bool(v < lit)
            if op == "le":
                return bool(v <= lit)
            if op == "gt":
                return bool(v > lit)
            if op == "ge":
                return bool(v >= lit)
        except TypeError:
            return False
        raise ODataSyntaxError(op)

    return pred


def compile_filter(expr: str | None) -> Predicate:
    if not expr:
        return lambda d: True
    return _Parser(expr).parse()
