"""Diagnosis engine: pure function from measurements to verdict."""
from .diagnose import SEGMENT_NAMES, SEGMENTS, TITLES, diagnose  # noqa: F401
from .features import GRADE_MEANING, extract, lag_grade  # noqa: F401
