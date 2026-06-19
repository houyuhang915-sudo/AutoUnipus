"""题型 handler 集合."""
from .base import FillResult, QuestionHandler
from .blank import BlankHandler
from .multi_choice import MultiChoiceHandler
from .registry import DEFAULT_HANDLERS, detect_all, fill_page
from .single_choice import SingleChoiceHandler
from .translation import TranslationHandler
from .word_blank import WordBlankHandler

__all__ = [
    "QuestionHandler",
    "FillResult",
    "SingleChoiceHandler",
    "MultiChoiceHandler",
    "BlankHandler",
    "WordBlankHandler",
    "TranslationHandler",
    "DEFAULT_HANDLERS",
    "detect_all",
    "fill_page",
]
