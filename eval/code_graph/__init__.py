"""Isolated quality and performance benchmark for the Python code graph."""

from .runner import BenchmarkGateError, run_benchmark

__all__ = ["BenchmarkGateError", "run_benchmark"]
