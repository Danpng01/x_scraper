"""Read local .env values without executing code or expanding secret contents."""
import os
import re

KEYS = {"GEMINI_API_KEY", "GEMINI_MODEL", "X_USERNAME", "X_EMAIL", "X_PASSWORD", "X_BEARER_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "LUMID_MODEL"}


def load_config(path):
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            if not sep or key.strip() not in KEYS:
                continue
            value = value.strip()
            if value.startswith(('"', "'")):
                quote = value[0]
                end = value.find(quote, 1)
                if end < 0:
                    raise ValueError(f"Unclosed quoted setting for {key.strip()} in {path.name}.")
                value = value[1:end]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            values[key.strip()] = value
    # Deployment/shell environment takes precedence over the local file.
    values.update({key: os.environ[key] for key in KEYS if key in os.environ})
    return values
