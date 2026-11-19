from collections.abc import Iterable
from dataclasses import dataclass

from catalog.codebook import Codebook


@dataclass(frozen=True)
class DictionaryEvidence:
    definition: Codebook

    @property
    def version(self) -> str:
        return self.definition.version


def freeze_dictionary_evidence(book: Codebook) -> DictionaryEvidence:
    return DictionaryEvidence(book)


def index_dictionary_evidence(declarations: Iterable[DictionaryEvidence]) -> dict[str, Codebook]:
    index = {}
    for declaration in declarations:
        version = declaration.version
        book = declaration.definition
        previous = index.get(version)
        if previous is not None and previous != book:
            raise ValueError(f'conflicting dictionary evidence for {version!r}')
        index[version] = book
    return index
