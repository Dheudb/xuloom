"""Extract bounded readable RTF text without executing embedded objects."""
import codecs
import re

TOKENS = re.compile(r"(\\'[0-9a-fA-F]{2})+|\\([a-zA-Z]+)(-?\d*) ?|\\([^a-zA-Z])|[{}]|[^\\{}]+")
SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer", "filetbl", "datastore", "themedata", "fldinst"}


def extract(data):
    if not data.startswith(b"{\\rtf"):
        raise ValueError("Invalid RTF file")
    source = data.decode("latin1")
    match = re.search(r"\\ansicpg(\d+)", source[:200])
    encoding = "cp" + match[1] if match else "cp1252"
    try:
        codecs.lookup(encoding)
    except LookupError:
        encoding = "cp1252"
    stack, ignored, fallback, pending, result = [], False, 1, 0, []
    for token in TOKENS.finditer(source):
        value, word, number, symbol = token[0], token[2], token[3], token[4]
        if value == "{":
            if len(stack) > 1000:
                raise ValueError("RTF nesting exceeds the preview limit")
            stack.append((ignored, fallback))
        elif value == "}":
            if stack:
                ignored, fallback = stack.pop()
        elif symbol == "*" or word in SKIP:
            ignored = True
        elif ignored:
            continue
        elif word == "uc":
            fallback = max(0, min(10, int(number or 1)))
        elif word == "u" and number:
            result.append(chr(int(number) % 65536))
            pending = fallback
        elif word in {"par", "line", "row"}:
            result.append("\n")
        elif word in {"tab", "cell"}:
            result.append("\t")
        elif word in {"emdash", "endash", "bullet", "lquote", "rquote", "ldblquote", "rdblquote"}:
            result.append({"emdash": "—", "endash": "–", "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”"}[word])
        elif token[1]:
            raw = bytes(int(pair, 16) for pair in re.findall(r"\\'([0-9a-fA-F]{2})", value))
            decoded = raw.decode(encoding, errors="replace")
            result.append(decoded[pending:])
            pending = max(0, pending - len(decoded))
        elif symbol in {"{", "}", "\\", "~", "_"}:
            result.append({"~": "\u00a0", "_": "‑"}.get(symbol, symbol) if not pending else "")
            pending = max(0, pending - 1)
        elif not value.startswith("\\"):
            decoded = value.replace("\r", "").replace("\n", "")
            result.append(decoded[pending:])
            pending = max(0, pending - len(decoded))
    return "".join(result).encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")
