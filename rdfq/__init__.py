from .terms import TermError
from .query import QueryError
from .store import StoreError, ManifestError, load_store, open_readonly, dataset_info
from .eval import EvaluationError, run_query

__all__ = [
    "TermError", "QueryError", "StoreError", "ManifestError", "EvaluationError",
    "load_store", "open_readonly", "dataset_info", "run_query",
]

__version__ = "1.0.0"
