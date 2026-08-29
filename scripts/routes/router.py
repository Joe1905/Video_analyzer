"""Small, dependency-free HTTP method and path matcher."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping
import re


_REGISTERED_METHODS = ("GET", "POST", "DELETE")
_METHOD_RE = re.compile(r"[A-Z]+")
_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class RouteError(ValueError):
    """Base class for invalid route definitions."""


class RouteConflictError(RouteError):
    """Raised when two equally-specific routes can match the same path."""


class RouteNotFound(LookupError):
    """Raised when no registered route pattern matches a path."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class MethodNotAllowed(LookupError):
    """Raised when a path exists but is not registered for the request method."""

    def __init__(self, path: str, allowed_methods: tuple[str, ...]) -> None:
        super().__init__(path)
        self.path = path
        self.allowed_methods = allowed_methods


@dataclass(frozen=True)
class RouteMatch:
    """A resolved handler and the path parameters captured for it."""

    handler: Callable[..., object]
    params: Mapping[str, str]


@dataclass(frozen=True)
class _Route:
    method: str
    pattern: str
    segments: tuple[str, ...]
    handler: Callable[..., object]
    literal_specificity: int


@dataclass(frozen=True)
class _PrefixRoute:
    method: str
    prefix: str
    handler: Callable[..., object]


def _validate_method(method: str, *, registering: bool) -> None:
    if not isinstance(method, str) or not _METHOD_RE.fullmatch(method):
        raise ValueError("method must be an uppercase HTTP token")
    if registering and method not in _REGISTERED_METHODS:
        raise ValueError("only GET, POST, and DELETE routes can be registered")


def _parse_pattern(pattern: str) -> tuple[tuple[str, ...], int]:
    _validate_path(pattern, name="pattern")
    segments = tuple(pattern.split("/")[1:])
    names: set[str] = set()
    literal_specificity = 0
    for segment in segments:
        match = _PARAM_RE.fullmatch(segment)
        if match:
            name = match.group(1)
            if name in names:
                raise ValueError("route parameter names must be unique")
            names.add(name)
        else:
            if "{" in segment or "}" in segment:
                raise ValueError("route parameters must occupy a complete segment")
            literal_specificity += 1
    return segments, literal_specificity


def _validate_path(path: str, *, name: str) -> None:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError(f"{name} must be a path beginning with '/'")
    if "?" in path or "#" in path:
        raise ValueError(f"{name} must not contain a query string or fragment")


def _validate_prefix(prefix: str) -> None:
    _validate_path(prefix, name="prefix")
    if prefix == "/" or not prefix.endswith("/"):
        raise ValueError("prefix must be a non-root path ending in '/'")
    if any(character in prefix for character in "{}*[]"):
        raise ValueError("prefix must be literal")


def _routes_overlap(left: _Route, right: _Route) -> bool:
    if len(left.segments) != len(right.segments):
        return False
    for left_segment, right_segment in zip(left.segments, right.segments):
        left_parameter = _PARAM_RE.fullmatch(left_segment)
        right_parameter = _PARAM_RE.fullmatch(right_segment)
        if not left_parameter and not right_parameter and left_segment != right_segment:
            return False
    return True


def _match(route: _Route, path_segments: tuple[str, ...]) -> dict[str, str] | None:
    if len(route.segments) != len(path_segments):
        return None
    params: dict[str, str] = {}
    for pattern_segment, path_segment in zip(route.segments, path_segments):
        parameter = _PARAM_RE.fullmatch(pattern_segment)
        if parameter:
            if not path_segment:
                return None
            params[parameter.group(1)] = path_segment
        elif pattern_segment != path_segment:
            return None
    return params


class Router:
    """Registry that resolves a method and raw parsed path without executing it."""

    def __init__(self) -> None:
        self._routes: list[_Route] = []
        self._prefix_routes: list[_PrefixRoute] = []

    def add(self, method: str, pattern: str, handler: Callable[..., object]) -> None:
        _validate_method(method, registering=True)
        if not callable(handler):
            raise TypeError("handler must be callable")
        segments, literal_specificity = _parse_pattern(pattern)
        candidate = _Route(method, pattern, segments, handler, literal_specificity)
        for route in self._routes:
            if (
                route.method == candidate.method
                and route.literal_specificity == candidate.literal_specificity
                and _routes_overlap(route, candidate)
            ):
                raise RouteConflictError(
                    f"conflicting {method} route: {pattern} overlaps {route.pattern}"
                )
        self._routes.append(candidate)

    def get(self, pattern: str, handler: Callable[..., object]) -> None:
        self.add("GET", pattern, handler)

    def get_prefix(self, prefix: str, handler: Callable[..., object]) -> None:
        """Register a literal GET prefix route with an un-decoded ``suffix`` param."""
        if not callable(handler):
            raise TypeError("handler must be callable")
        _validate_prefix(prefix)
        if any(route.prefix == prefix for route in self._prefix_routes):
            raise RouteConflictError(f"conflicting GET prefix route: {prefix}")
        self._prefix_routes.append(_PrefixRoute("GET", prefix, handler))

    def post(self, pattern: str, handler: Callable[..., object]) -> None:
        self.add("POST", pattern, handler)

    def delete(self, pattern: str, handler: Callable[..., object]) -> None:
        self.add("DELETE", pattern, handler)

    def resolve(self, method: str, raw_path: str) -> RouteMatch:
        _validate_method(method, registering=False)
        _validate_path(raw_path, name="path")
        path_segments = tuple(raw_path.split("/")[1:])
        matches: list[tuple[_Route, dict[str, str]]] = []
        allowed: set[str] = set()
        for route in self._routes:
            params = _match(route, path_segments)
            if params is None:
                continue
            allowed.add(route.method)
            if route.method == method:
                matches.append((route, params))
        if matches:
            route, params = max(matches, key=lambda item: item[0].literal_specificity)
            return RouteMatch(route.handler, MappingProxyType(params))
        if allowed:
            allowed_methods = tuple(
                registered for registered in _REGISTERED_METHODS if registered in allowed
            )
            raise MethodNotAllowed(raw_path, allowed_methods)
        prefix_matches = [
            route for route in self._prefix_routes if raw_path.startswith(route.prefix)
        ]
        matching_prefixes = [
            route for route in prefix_matches if route.method == method
        ]
        if matching_prefixes:
            route = max(matching_prefixes, key=lambda candidate: len(candidate.prefix))
            return RouteMatch(
                route.handler,
                MappingProxyType({"suffix": raw_path.removeprefix(route.prefix)}),
            )
        if prefix_matches:
            allowed_methods = tuple(
                registered
                for registered in _REGISTERED_METHODS
                if any(route.method == registered for route in prefix_matches)
            )
            raise MethodNotAllowed(raw_path, allowed_methods)
        raise RouteNotFound(raw_path)
