import traceback
from exllamav2 import ExLlamaV2, ExLlamaV2Tokenizer
from exllamav2.generator.filters import ExLlamaV2Filter, ExLlamaV2PrefixFilter
from lmformatenforcer import (
    JsonSchemaParser,
    RegexParser,
    TokenEnforcer,
    CharacterLevelParser,
)
from lmformatenforcer.integrations.exllamav2 import (
    build_token_enforcer_tokenizer_data,
)
from loguru import logger
from typing import List, Type
from functools import lru_cache
from pydantic import BaseModel


class OutlinesTokenizerWrapper:
    """Wrapper for Outlines tokenizer"""

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        id_to_piece = self.tokenizer.get_id_to_piece_list()
        self.vocabulary = {piece: idx for idx, piece in enumerate(id_to_piece)}
        self.eos_token_id = self.tokenizer.eos_token_id
        self.eos_token = id_to_piece[self.tokenizer.eos_token_id]
        self.special_tokens = list(self.tokenizer.extended_id_to_piece.keys())

    def convert_token_to_string(self, token):
        return token

    def decode(self, tokens):
        s = ""
        id_to_piece = self.tokenizer.get_id_to_piece_list()
        for t in tokens:
            s += id_to_piece[t]
        return s


class ExLlamaV2EbnfFilter(ExLlamaV2Filter):
    """Filter class for context-free grammar via outlines"""

    def __init__(self, model, tokenizer, grammar):
        from outlines.fsm.fsm import CFGFSM

        super().__init__(model, tokenizer)

        self.wrapped_tokenizer = OutlinesTokenizerWrapper(tokenizer)
        self.fsm = CFGFSM(grammar, self.wrapped_tokenizer)
        self.state = self.fsm.first_state

    def begin(self, prefix_str=""):
        self.state = self.fsm.first_state

    def feed(self, token):
        self.state = self.fsm.next_state(self.state, token.item())

    def next(self):
        return self.fsm.allowed_token_ids(self.state), set()

    def use_background_worker(self):
        return True


@lru_cache(10)
def _get_lmfe_tokenizer_data(tokenizer: ExLlamaV2Tokenizer):
    return build_token_enforcer_tokenizer_data(tokenizer)


class ExLlamaV2TokenEnforcerFilter(ExLlamaV2Filter):
    """Filter class for LMFE"""

    token_sequence: List[int]

    def __init__(
        self,
        model: ExLlamaV2,
        tokenizer: ExLlamaV2Tokenizer,
        character_level_parser: CharacterLevelParser,
    ):
        super().__init__(model, tokenizer)
        tokenizer_data = _get_lmfe_tokenizer_data(tokenizer)
        self.token_enforcer = TokenEnforcer(tokenizer_data, character_level_parser)
        self.token_sequence = []

    def begin(self, prefix_str: str):
        self.token_sequence = []

    def feed(self, token):
        self.token_sequence.append(int(token[0][0]))

    def next(self):
        allowed_tokens = self.token_enforcer.get_allowed_tokens(self.token_sequence)
        if not hasattr(self, "allow_return_type_list"):
            return set(allowed_tokens), set()
        else:
            return sorted(allowed_tokens), []

    def use_background_worker(self):
        return True


def clear_grammar_func_cache():
    """Flush tokenizer_data cache to avoid holding references to
    tokenizers after unloading a model"""

    _get_lmfe_tokenizer_data.cache_clear()


class ExLlamaV2Grammar:
    """ExLlamaV2 class for various grammar filters/parsers."""

    filters: List[ExLlamaV2Filter]

    def __init__(self):
        self.filters = []

    def add_json_schema_filter(
        self,
        json_schema: dict,
        model: ExLlamaV2,
        tokenizer: ExLlamaV2Tokenizer,
    ):
        """Adds an ExllamaV2 filter based on a JSON schema."""

        # Create the parser
        try:
            schema_parser = JsonSchemaParser(json_schema)
        except Exception:
            traceback.print_exc()
            logger.error(
                "Skipping because the JSON schema couldn't be parsed. "
                "Please read the above error for more information."
            )

            return

        # Allow JSON objects or JSON arrays at the top level
        json_prefixes = ["[", "{"]

        lmfilter = ExLlamaV2TokenEnforcerFilter(model, tokenizer, schema_parser)
        prefix_filter = ExLlamaV2PrefixFilter(model, tokenizer, json_prefixes)

        # Append the filters
        self.filters.extend([lmfilter, prefix_filter])

    def add_regex_filter(
        self,
        pattern: str,
        model: ExLlamaV2,
        tokenizer: ExLlamaV2Tokenizer,
    ):
        """Adds an ExllamaV2 filter based on regular expressions."""

        # Create the parser
        try:
            pattern_parser = RegexParser(pattern)
        except Exception:
            traceback.print_exc()
            logger.error(
                "Skipping because the regex pattern couldn't be parsed. "
                "Please read the above error for more information."
            )

            return

        lmfilter = ExLlamaV2TokenEnforcerFilter(model, tokenizer, pattern_parser)

        # Append the filters
        self.filters.append(lmfilter)

    def add_ebnf_filter(
        self,
        ebnf_string: str,
        model: ExLlamaV2,
        tokenizer: ExLlamaV2Tokenizer,
    ):
        """
        Add an EBNF grammar filter.
        Possibly replace outlines with an in-house solution in the future.
        """

        try:
            ebnf_filter = ExLlamaV2EbnfFilter(model, tokenizer, ebnf_string)
        except ImportError:
            logger.error(
                "Skipping EBNF parsing because Outlines is not installed.\n"
                "Please run the following command in your environment "
                "to install extra packages:\n"
                "pip install -U .[extras]"
            )

            return

        self.filters.append(ebnf_filter)
