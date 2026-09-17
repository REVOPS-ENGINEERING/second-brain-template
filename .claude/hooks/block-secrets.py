"""
PreToolUse hook: Block access to sensitive files and environment variables.

Intercepts Read, Bash, Grep, Edit, Write, and Glob tool calls to prevent
API keys, tokens, and credentials from entering the LLM context window.

Exit codes:
  0 = allow (tool proceeds normally)
  2 = block (stderr shown to Claude as feedback)
"""

import json
import re
import sys

# --- Sensitive file patterns ---
SENSITIVE_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.env($|[./])|\.envrc$", re.IGNORECASE), # .env, .env.local, .env/, .envrc
    re.compile(r"\.pem$", re.IGNORECASE),                 # SSL/TLS certificates
    re.compile(r"\.key$", re.IGNORECASE),                 # Private keys
    re.compile(r"\.p12$", re.IGNORECASE),                 # PKCS#12 keystores
    re.compile(r"\.pfx$", re.IGNORECASE),                 # PFX keystores
    re.compile(r"\.mcp\.json", re.IGNORECASE),            # MCP config with interpolated secrets
    re.compile(r"msal_cache\.bin", re.IGNORECASE),        # MSAL token cache
    re.compile(r"google_credentials\.json", re.IGNORECASE),
    re.compile(r"google_token\.json", re.IGNORECASE),
    re.compile(r"credentials\.json", re.IGNORECASE),
    re.compile(r"token\.json", re.IGNORECASE),
    re.compile(r"master\.env", re.IGNORECASE),
    re.compile(r"\.ssh/", re.IGNORECASE),
    re.compile(r"id_rsa", re.IGNORECASE),
    re.compile(r"id_ed25519", re.IGNORECASE),
    re.compile(r"\.aws/credentials", re.IGNORECASE),
    re.compile(r"\.config/gcloud/", re.IGNORECASE),
    re.compile(r"\.docker/config\.json", re.IGNORECASE),
    re.compile(r"\.netrc", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
]

# Exclude false positives for generic patterns (secret, password, token)
# Code and docs that DISCUSS secrets are safe; actual secret FILES are not.
# Deliberately NOT allowlisting .json — files like my_secrets.json could be real.
SAFE_EXTENSIONS: list[re.Pattern[str]] = [
    re.compile(r"\.md$", re.IGNORECASE),
    re.compile(r"\.py$", re.IGNORECASE),
    re.compile(r"\.ts$", re.IGNORECASE),
    re.compile(r"\.js$", re.IGNORECASE),
    re.compile(r"\.txt$", re.IGNORECASE),
    re.compile(r"\.yml$", re.IGNORECASE),
    re.compile(r"\.yaml$", re.IGNORECASE),
    re.compile(r"\.toml$", re.IGNORECASE),
    re.compile(r"\.example$", re.IGNORECASE),
    re.compile(r"\.sample$", re.IGNORECASE),
]

# These generic patterns use the safe-extension carveout
GENERIC_PATTERNS = {"secret", "password", "token"}


def is_sensitive_file(path: str) -> str | None:
    """Check if a file path matches a sensitive pattern. Returns reason or None."""
    for pattern in SENSITIVE_FILE_PATTERNS:
        if pattern.search(path):
            # Generic patterns: allow code/doc files that discuss secrets
            if pattern.pattern in GENERIC_PATTERNS:
                for ext in SAFE_EXTENSIONS:
                    if ext.search(path):
                        return None
                # Also allow .env.example / .env.sample explicitly
                lower = path.lower()
                if ".example" in lower or ".sample" in lower:
                    return None
            # Allow .env.example and .env.sample for the .env pattern
            if pattern.pattern == r"\.env($|[./])|\.envrc$":
                lower = path.lower()
                if ".env.example" in lower or ".env.sample" in lower:
                    return None
            return f"Blocked: '{path}' matches sensitive file pattern '{pattern.pattern}'"
    return None


# --- Dangerous bash patterns ---
DANGEROUS_BASH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # A. Direct .env file reads
    (re.compile(r"\bcat\b.*\.env\b", re.IGNORECASE), "Reading .env file with cat"),
    (re.compile(r"\bhead\b.*\.env\b", re.IGNORECASE), "Reading .env file with head"),
    (re.compile(r"\btail\b.*\.env\b", re.IGNORECASE), "Reading .env file with tail"),
    (re.compile(r"\bless\b.*\.env\b", re.IGNORECASE), "Reading .env file with less"),
    (re.compile(r"\bmore\b.*\.env\b", re.IGNORECASE), "Reading .env file with more"),
    (re.compile(r"\bbat\b.*\.env\b", re.IGNORECASE), "Reading .env file with bat"),
    (re.compile(r"\bvi\b.*\.env\b", re.IGNORECASE), "Opening .env file in editor"),
    (re.compile(r"\bvim\b.*\.env\b", re.IGNORECASE), "Opening .env file in editor"),
    (re.compile(r"\bnano\b.*\.env\b", re.IGNORECASE), "Opening .env file in editor"),
    (re.compile(r"\bcode\b.*\.env\b", re.IGNORECASE), "Opening .env file in editor"),
    (re.compile(r"\bsource\b.*\.env\b", re.IGNORECASE), "Sourcing .env file"),
    (re.compile(r"\.\s+.*\.env\b", re.IGNORECASE), "Sourcing .env with dot notation"),

    # B. Credential file reads
    (re.compile(r"\bcat\b.*credentials", re.IGNORECASE), "Reading credentials file"),
    (re.compile(r"\bcat\b.*google_token", re.IGNORECASE), "Reading Google token file"),
    (re.compile(r"\bcat\b.*\.pem\b", re.IGNORECASE), "Reading certificate file"),
    (re.compile(r"\bcat\b.*\.key\b", re.IGNORECASE), "Reading key file"),
    (re.compile(r"\bcat\b.*id_rsa", re.IGNORECASE), "Reading SSH private key"),
    (re.compile(r"\bcat\b.*id_ed25519", re.IGNORECASE), "Reading SSH private key"),
    (re.compile(r"\bcat\b.*\.ssh/", re.IGNORECASE), "Reading SSH directory file"),
    (re.compile(r"\bcat\b.*master\.env", re.IGNORECASE), "Reading master env file"),
    (re.compile(r"\bcat\b.*msal_cache", re.IGNORECASE), "Reading MSAL token cache"),

    # C. Environment variable printing
    (re.compile(r"\bprintenv\b", re.IGNORECASE), "Printing environment variables"),
    (re.compile(r"\benv\b\s*$", re.IGNORECASE), "Listing all environment variables"),
    (re.compile(r"\benv\b\s*\|", re.IGNORECASE), "Piping environment variables"),
    (re.compile(r"\bset\b\s*\|", re.IGNORECASE), "Piping shell variables"),
    (re.compile(r"\bexport\s+-p\b", re.IGNORECASE), "Listing exported variables"),
    (re.compile(r"\bdeclare\s+-x\b", re.IGNORECASE), "Listing exported variables"),
    (re.compile(r"\bcompgen\s+-v\b", re.IGNORECASE), "Listing all variable names"),

    # D. Secret variable echoing
    (re.compile(r"\becho\b.*\$.*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|ACCESS_TOKEN|AUTH)", re.IGNORECASE),
     "Echoing secret environment variable"),
    (re.compile(r"\bprintf\b.*\$.*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|ACCESS_TOKEN|AUTH)", re.IGNORECASE),
     "Printf of secret environment variable"),

    # E. Interpreter inline execution
    (re.compile(r"python[3]?\s+-c\s+.*os\.environ", re.IGNORECASE),
     "Python inline code accessing os.environ"),
    (re.compile(r"python[3]?\s+-c\s+.*os\.getenv", re.IGNORECASE),
     "Python inline code accessing os.getenv"),
    (re.compile(r"python[3]?\s+-c\s+.*dotenv", re.IGNORECASE),
     "Python inline code loading dotenv"),
    (re.compile(r"python[3]?\s+-c\s+.*\.env", re.IGNORECASE),
     "Python inline code referencing .env"),
    (re.compile(r"python[3]?\s+-c\s+.*open\(.*\.env", re.IGNORECASE),
     "Python inline code opening .env"),
    (re.compile(r"node\s+-e\s+.*process\.env", re.IGNORECASE),
     "Node inline code accessing process.env"),
    (re.compile(r"\bruby\s+-e\b.*ENV", re.IGNORECASE),
     "Ruby inline code accessing ENV"),
    (re.compile(r"\bperl\s+-e\b.*ENV", re.IGNORECASE),
     "Perl inline code accessing %ENV"),
    (re.compile(r"\bphp\s+-r\b.*getenv", re.IGNORECASE),
     "PHP inline code accessing getenv"),

    # F. Grep/search targeting sensitive files
    (re.compile(r"\bgrep\b.*\.env\b", re.IGNORECASE), "Grep searching .env file"),
    (re.compile(r"\brg\b.*\.env\b", re.IGNORECASE), "Ripgrep searching .env file"),
    (re.compile(r"\bfind\b.*\.env\b", re.IGNORECASE), "Find searching for .env files"),
    (re.compile(r"\bfind\b.*-exec\b.*cat", re.IGNORECASE), "Find with exec cat"),

    # G. Wildcard and glob bypasses
    (re.compile(r"\bcat\b.*\.en\*", re.IGNORECASE), "Wildcard read that could match .env"),
    (re.compile(r"\bcat\b.*\.e\?\?", re.IGNORECASE), "Wildcard read that could match .env"),
    (re.compile(r"\bcat\b.*\.e\[", re.IGNORECASE), "Glob pattern read that could match .env"),
    (re.compile(r"\bcat\b.*\.e\$", re.IGNORECASE), "Variable expansion bypass targeting .env"),
    (re.compile(r"\bcat\b.*\.e\\", re.IGNORECASE), "Backslash bypass targeting .env"),

    # H. Symlink/copy operations
    (re.compile(r"\bln\b.*-s.*\.env\b", re.IGNORECASE), "Creating symlink to .env file"),
    (re.compile(r"\bln\b.*-s.*credentials", re.IGNORECASE), "Creating symlink to credentials"),
    (re.compile(r"\bln\b.*-s.*google_token", re.IGNORECASE), "Creating symlink to token file"),
    (re.compile(r"\bcp\b.*\.env\b", re.IGNORECASE), "Copying .env file"),

    # I. Here-doc/here-string execution
    (re.compile(r"python[3]?\s*<<", re.IGNORECASE),
     "Python here-doc execution (could access env vars)"),
    (re.compile(r"\bperl\b\s*<<", re.IGNORECASE),
     "Perl here-doc execution (could access env vars)"),
    (re.compile(r"\bruby\b\s*<<", re.IGNORECASE),
     "Ruby here-doc execution (could access env vars)"),

    # J. Base64 decode bypass
    (re.compile(r"base64\s+(-d|--decode).*\|\s*(sh|bash|zsh|python|ruby|perl|node)", re.IGNORECASE),
     "Base64 decoded command piped to interpreter"),
    (re.compile(r"bash\s*<<<.*base64", re.IGNORECASE),
     "Base64 here-string piped to bash"),

    # K. Eval/exec dangerous patterns
    (re.compile(r"\beval\b.*\.env", re.IGNORECASE), "Eval referencing .env file"),
    (re.compile(r"\beval\b.*\$.*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE),
     "Eval referencing secret variable"),
    (re.compile(r"\beval\b.*os\.environ", re.IGNORECASE), "Eval accessing os.environ"),
    (re.compile(r"\bexec\b\s+\d*[<>]", re.IGNORECASE), "Exec with file descriptor redirect"),

    # L. Curl/wget exfiltration
    (re.compile(r"\bcurl\b.*\$.*(?:KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE),
     "Curl with secret variable in URL/data"),
    (re.compile(r"\bwget\b.*\$.*(?:KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE),
     "Wget with secret variable in URL/data"),
    (re.compile(r"\bcurl\b.*-d\s*@.*\.env", re.IGNORECASE),
     "Curl posting .env file contents"),
    (re.compile(r"\bcurl\b.*--data.*\.env", re.IGNORECASE),
     "Curl posting .env file contents"),

    # M. Process substitution
    (re.compile(r"<\(.*cat.*\.env", re.IGNORECASE), "Process substitution reading .env"),
    (re.compile(r"<\(.*\.env", re.IGNORECASE), "Process substitution referencing .env"),

    # N. Binary dumps
    (re.compile(r"\bxxd\b.*\.env", re.IGNORECASE), "Hex dump of .env file"),
    (re.compile(r"\bhexdump\b.*\.env", re.IGNORECASE), "Hex dump of .env file"),
    (re.compile(r"\bod\b.*\.env", re.IGNORECASE), "Octal dump of .env file"),

    # O. xargs exploitation
    (re.compile(r"\bxargs\b.*cat", re.IGNORECASE), "xargs with cat (potential .env read)"),
]


def check_bash_command(command: str) -> str | None:
    """Check if a bash command would expose secrets. Returns reason or None."""
    normalized = " ".join(command.split()).strip()

    for pattern, reason in DANGEROUS_BASH_PATTERNS:
        if pattern.search(normalized):
            return f"Blocked: {reason}"

    # P. Subshell recursion: extract $(...) and `...` content, re-check
    subshell_patterns = [
        re.compile(r"\$\((.*?)\)", re.DOTALL),
        re.compile(r"`(.*?)`", re.DOTALL),
    ]
    for sp in subshell_patterns:
        for match in sp.finditer(normalized):
            inner = match.group(1)
            result = check_bash_command(inner)
            if result:
                return f"{result} (inside subshell)"

    return None


# --- Two-step attack: content patterns that would exfiltrate secrets ---
EXFILTRATION_CONTENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Python: printing env vars
    (re.compile(r"print\s*\(.*os\.environ", re.IGNORECASE),
     "Script prints os.environ to stdout"),
    (re.compile(r"print\s*\(.*os\.getenv\s*\(", re.IGNORECASE),
     "Script prints os.getenv() to stdout"),
    (re.compile(r"json\.dumps?\s*\(.*os\.environ", re.IGNORECASE),
     "Script serializes os.environ to JSON"),
    (re.compile(r"sys\.stdout\.write.*os\.environ", re.IGNORECASE),
     "Script writes os.environ to stdout"),
    (re.compile(r"pprint.*os\.environ", re.IGNORECASE),
     "Script pretty-prints os.environ"),
    # Python: reading .env and printing
    (re.compile(r"open\s*\(.*\.env.*\).*read\(\)", re.IGNORECASE),
     "Script reads .env file contents"),
    # Bash script: cat/echo env vars
    (re.compile(r"cat\s+.*\.env", re.IGNORECASE),
     "Script cats .env file"),
    (re.compile(r"echo\s+\$\{?[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE),
     "Script echoes secret variable"),
    (re.compile(r"printenv", re.IGNORECASE),
     "Script runs printenv"),
    # Ruby/Perl/Node
    (re.compile(r"puts\s+ENV", re.IGNORECASE), "Script prints Ruby ENV"),
    (re.compile(r"print\s+%ENV", re.IGNORECASE), "Script prints Perl %ENV"),
    (re.compile(r"console\.log\s*\(\s*process\.env", re.IGNORECASE), "Script logs process.env"),
    # HTTP exfiltration in script
    (re.compile(r"curl.*\$\{?[A-Z_]*(?:KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE),
     "Script exfiltrates secret via curl"),
    (re.compile(r"requests\.(get|post).*os\.getenv", re.IGNORECASE),
     "Script sends env var via HTTP request"),
]


def check_written_content(content: str) -> str | None:
    """Check if file content being written would exfiltrate secrets."""
    if not content:
        return None
    for pattern, reason in EXFILTRATION_CONTENT_PATTERNS:
        if pattern.search(content):
            return f"Blocked: {reason} — writing scripts that expose secrets is not allowed"
    return None


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("HOOK ERROR (fail-closed): malformed hook input JSON", file=sys.stderr)
        sys.exit(2)

    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    reason: str | None = None

    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        reason = is_sensitive_file(file_path)

    elif tool_name == "Bash":
        command = tool_input.get("command", "")
        reason = check_bash_command(command)

    elif tool_name == "Grep":
        path = tool_input.get("path", "")
        pattern = tool_input.get("pattern", "")
        glob_str = tool_input.get("glob", "")
        if path:
            reason = is_sensitive_file(path)
        if not reason and re.search(r"\.env", glob_str or "", re.IGNORECASE):
            reason = "Blocked: Grep glob targeting .env files"
        if not reason and re.search(r"\.env", pattern or "", re.IGNORECASE):
            reason = "Blocked: Grep pattern targeting .env files"

    elif tool_name in ("Edit", "Write"):
        file_path = tool_input.get("file_path", "")
        reason = is_sensitive_file(file_path)
        if not reason:
            # Skip exfiltration checks for test files — they legitimately
            # contain strings like "os.environ" as test fixtures/assertions.
            is_test_file = "/tests/" in file_path or file_path.startswith("tests/")
            if not is_test_file:
                content = tool_input.get("content", "") or tool_input.get("new_string", "")
                reason = check_written_content(content)

    elif tool_name == "Glob":
        pattern_str = tool_input.get("pattern", "")
        if re.search(r"\.env", pattern_str, re.IGNORECASE):
            reason = "Blocked: Glob pattern targeting .env files"

    if reason:
        print(
            f"SECURITY: {reason}. "
            "API keys and credentials must never enter the context window.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Fail CLOSED — a crashed security hook must block the tool call,
        # not silently allow it (ratified 2026-07-03).
        print(f"HOOK ERROR (fail-closed): {exc}", file=sys.stderr)
        sys.exit(2)
