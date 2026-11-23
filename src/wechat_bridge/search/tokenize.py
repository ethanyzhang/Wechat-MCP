"""NFKC/casefolded literal matching; original text is never rewritten."""
import unicodedata


def normalize(text):
    """NFKC, casefold and collapse whitespace; queries and texts are compared in this form."""
    return ' '.join(unicodedata.normalize('NFKC', unicodedata.normalize('NFKC', text).casefold()).split())


def _source_chunks(text):
    """Canonical composition units with their original source start offset.

    Compatibility expansion can produce several units from one source character.
    Combining marks and composable Hangul Jamo stay with their starter, so NFC
    and case folding do not split a source composition across chunk boundaries.
    Only one composition unit is buffered, rather than a map for the whole text.
    """
    pending, start = [], 0
    for index, char in enumerate(text):
        for part in unicodedata.normalize('NFKD', char):
            if pending and unicodedata.combining(part) == 0:
                composed = unicodedata.normalize('NFC', ''.join(pending))
                joins = len(composed) == 1 and len(unicodedata.normalize('NFC', composed + part)) == 1
                if not joins:
                    yield unicodedata.normalize('NFKC', composed.casefold()), start
                    pending = []
            if not pending:
                start = index
            pending.append(part)
    if pending:
        yield unicodedata.normalize('NFKC', unicodedata.normalize('NFC', ''.join(pending)).casefold()), start


def source_offset(text, normalized_offset):
    """Map a normalized code-point position to its original composition unit."""
    position, whitespace, seen = 0, None, False
    for chunk, start in _source_chunks(text):
        for char in chunk:
            if char.isspace():
                if whitespace is None:
                    whitespace = start
                continue
            if whitespace is not None and seen:
                if position == normalized_offset:
                    return whitespace
                position += 1
            whitespace = None
            if position == normalized_offset:
                return start
            position += 1
            seen = True
    return len(text)
