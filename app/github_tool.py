"""
app/github_tool.py
------------------
The single agent tool for all GitHub interactions.

All GitHub REST/GraphQL calls live here with strict output shaping, hard size caps,
and guaranteed string returns (errors are formatted as informative strings,
never raised as exceptions into the agent loop).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Tunable Constants                                                           #
# --------------------------------------------------------------------------- #

REPO_LIST_CAP: int = 30
README_CHAR_CAP: int = 4_000
FILE_TREE_MAX_DEPTH: int = 3
FILE_TREE_MAX_ENTRIES: int = 200
FILE_CONTENT_CHAR_CAP: int = 6_000
RECENT_ACTIVITY_CAP: int = 10

IGNORED_TREE_NAMES: set[str] = {
    "node_modules",
    "dist",
    "build",
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "vendor",
    ".next",
    "coverage",
    ".cache",
}

BINARY_EXTENSIONS: set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".wasm",
    ".ttf",
    ".woff",
    ".woff2",
    ".eot",
    ".mp4",
    ".mp3",
    ".pyc",
}

REPO_SCOPED_ACTIONS: set[str] = {
    "get_repo_details",
    "get_readme",
    "get_file_tree",
    "get_file_content",
    "get_languages",
}

ActionType = Literal[
    "list_repos",
    "get_repo_details",
    "get_readme",
    "get_file_tree",
    "get_file_content",
    "get_languages",
    "get_profile",
    "get_pinned_repos",
    "get_recent_activity",
]

# --------------------------------------------------------------------------- #
# OpenAI-Compatible Tool Schema Constant                                      #
# --------------------------------------------------------------------------- #

GITHUB_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "github_lookup",
        "description": "Read-only lookup against a single public GitHub profile. Cannot write, cannot access private data.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_repos",
                        "get_repo_details",
                        "get_readme",
                        "get_file_tree",
                        "get_file_content",
                        "get_languages",
                        "get_profile",
                        "get_pinned_repos",
                        "get_recent_activity",
                    ],
                    "description": "The GitHub action to perform.",
                },
                "repo": {
                    "type": "string",
                    "description": "Repo name, required for repo-scoped actions.",
                },
                "path": {
                    "type": "string",
                    "description": "File path, required for get_file_content.",
                },
            },
            "required": ["action"],
        },
    },
}

# --------------------------------------------------------------------------- #
# Pydantic Input Model                                                        #
# --------------------------------------------------------------------------- #


class GithubLookupArgs(BaseModel):
    """Validates raw tool arguments from the LLM before any HTTP call."""

    model_config = ConfigDict(extra="ignore")

    action: ActionType = Field(..., description="The GitHub action to execute.")
    repo: str | None = Field(default=None, description="Repository name.")
    path: str | None = Field(default=None, description="File path within repository.")

    @model_validator(mode="after")
    def validate_action_requirements(self) -> GithubLookupArgs:
        if self.action in REPO_SCOPED_ACTIONS and not self.repo:
            raise ValueError(f"Action '{self.action}' requires the 'repo' parameter.")
        if self.action == "get_file_content" and not self.path:
            raise ValueError("Action 'get_file_content' requires the 'path' parameter.")
        return self


# --------------------------------------------------------------------------- #
# GitHub Tool Class                                                           #
# --------------------------------------------------------------------------- #


class GithubTool:
    """Tool wrapper executing read-only GitHub actions."""

    def __init__(
        self,
        username: str,
        token: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._username = username.strip().lower()
        self._token = token.strip()

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"portfolio-agent/{self._username}",
        }

        self._client = client or httpx.Client(
            base_url="https://api.github.com",
            headers=headers,
            timeout=10.0,
        )

    def __repr__(self) -> str:
        return f"GithubTool(username={self._username!r})"

    def __str__(self) -> str:
        return f"GithubTool({self._username})"

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> GithubTool:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ----------------------------------------------------------------------- #
    # Guards & Helpers                                                        #
    # ----------------------------------------------------------------------- #

    def _is_own_repo(self, repo: str) -> bool:
        """Check if the repo belongs to the configured user.

        Bare repo names ('my-repo') are assumed to belong to this user.
        Prefixed names ('owner/my-repo') must match _username case-insensitively.
        """
        clean_repo = repo.strip()
        if "/" in clean_repo:
            owner, _ = clean_repo.split("/", 1)
            return owner.strip().lower() == self._username
        return True

    def _clean_repo_name(self, repo: str) -> str:
        """Extract the bare repository name from an optional 'owner/repo' string."""
        clean_repo = repo.strip()
        if "/" in clean_repo:
            _, repo_name = clean_repo.split("/", 1)
            return repo_name.strip()
        return clean_repo

    def _format_error(self, action: str, detail: str) -> str:
        """Return a uniform, descriptive error string for tool responses."""
        return f"[Tool error] GitHub '{action}' failed: {detail}"

    def _decode_base64_content(
        self,
        content_b64: str,
        encoding: str,
        action: str,
    ) -> tuple[str | None, str | None]:
        """Safely decode base64 encoded content.

        Returns (decoded_string, error_message).
        """
        if encoding != "base64":
            return None, self._format_error(
                action,
                f"Unexpected content encoding: '{encoding}'. Expected 'base64'.",
            )
        try:
            # Strip whitespace before decoding base64 payload
            cleaned = content_b64.strip()
            decoded = base64.b64decode(cleaned).decode("utf-8", errors="replace")
            return decoded, None
        except Exception as exc:
            return None, self._format_error(action, f"Failed to decode base64 content: {exc}")

    def _get(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[Any | None, str | None]:
        """Perform a GET request, returning (parsed_json, error_message).

        Never raises exceptions — handles 404, 422, rate limits, and network errors gracefully.
        """
        try:
            response = self._client.get(endpoint, params=params)

            # Inspect rate limit header proactively
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining is not None:
                try:
                    rem_count = int(remaining)
                    if rem_count < 10:
                        reset_time = response.headers.get("x-ratelimit-reset", "unknown")
                        logger.warning(
                            "GitHub rate limit low: %d requests remaining (resets at %s timestamp)",
                            rem_count,
                            reset_time,
                        )
                except ValueError:
                    pass

            if response.status_code == 200:
                return response.json(), None
            if response.status_code == 404:
                logger.info("GitHub resource not found (404) at %s", endpoint)
                return None, f"Resource not found (404) at '{endpoint}'."
            if response.status_code == 422:
                logger.info("GitHub unprocessable entity or empty repository (422) at %s", endpoint)
                return None, f"Resource unprocessable or repository is empty (422) at '{endpoint}'."
            if response.status_code in (403, 429):
                logger.warning(
                    "GitHub rate limit or forbidden (%d) at %s: %s",
                    response.status_code,
                    endpoint,
                    response.text,
                )
                return None, f"Rate limit reached or access forbidden ({response.status_code})."

            logger.warning(
                "GitHub returned HTTP %d for %s: %s",
                response.status_code,
                endpoint,
                response.text[:200],
            )
            return None, f"GitHub returned HTTP {response.status_code}."
        except httpx.HTTPError as exc:
            logger.warning("HTTP error during GET %s: %s", endpoint, exc)
            return None, f"Network error: {exc}"
        except Exception as exc:
            logger.error("Unexpected error during GET %s: %s", endpoint, exc, exc_info=True)
            return None, f"Request error: {exc}"

    # ----------------------------------------------------------------------- #
    # Action Handlers                                                         #
    # ----------------------------------------------------------------------- #

    def _list_repos(self) -> str:
        data, err = self._get(
            f"/users/{self._username}/repos",
            params={"type": "public", "sort": "updated", "per_page": 100},
        )
        if err or not isinstance(data, list):
            return self._format_error("list_repos", err or "Invalid response format.")

        # Explicit defense in depth: filter private repos
        public_repos = [r for r in data if not r.get("private", False)]
        total_count = len(public_repos)
        capped_repos = public_repos[:REPO_LIST_CAP]

        shaped_repos = [
            {
                "name": r.get("name"),
                "description": r.get("description"),
                "language": r.get("language"),
                "stars": r.get("stargazers_count", 0),
                "updated_at": r.get("updated_at"),
            }
            for r in capped_repos
        ]

        result: dict[str, Any] = {
            "total_public_repos_found": total_count,
            "displayed_count": len(shaped_repos),
            "repos": shaped_repos,
        }
        if total_count > REPO_LIST_CAP:
            result["_note"] = (
                f"Showing top {REPO_LIST_CAP} of {total_count} public repositories, "
                f"sorted by last updated."
            )

        return json.dumps(result, indent=2)

    def _get_repo_details(self, repo: str) -> str:
        repo_name = self._clean_repo_name(repo)
        data, err = self._get(f"/repos/{self._username}/{repo_name}")
        if err or not isinstance(data, dict):
            return self._format_error("get_repo_details", err or "Invalid response format.")

        if data.get("private", False):
            return self._format_error(
                "get_repo_details",
                f"Repository '{repo_name}' is private or inaccessible.",
            )

        shaped = {
            "name": data.get("name"),
            "description": data.get("description"),
            "language": data.get("language"),
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "open_issues": data.get("open_issues_count", 0),
            "topics": data.get("topics", []),
            "homepage": data.get("homepage"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
        }
        return json.dumps(shaped, indent=2)

    def _get_readme(self, repo: str) -> str:
        repo_name = self._clean_repo_name(repo)
        data, err = self._get(f"/repos/{self._username}/{repo_name}/readme")
        if err or not isinstance(data, dict):
            return self._format_error("get_readme", err or "README not found or inaccessible.")

        content_b64 = data.get("content", "")
        encoding = data.get("encoding", "")
        decoded, decode_err = self._decode_base64_content(content_b64, encoding, "get_readme")
        if decode_err:
            return decode_err
        if decoded is None:
            decoded = ""

        if len(decoded) > README_CHAR_CAP:
            return (
                decoded[:README_CHAR_CAP]
                + f"\n\n[Truncated: README exceeds {README_CHAR_CAP} characters]"
            )
        return decoded

    def _get_file_tree(self, repo: str) -> str:
        repo_name = self._clean_repo_name(repo)
        data, err = self._get(
            f"/repos/{self._username}/{repo_name}/git/trees/HEAD",
            params={"recursive": "1"},
        )
        if err or not isinstance(data, dict):
            return self._format_error("get_file_tree", err or "Tree not found or inaccessible.")

        tree_items = data.get("tree", [])
        if not isinstance(tree_items, list):
            return self._format_error("get_file_tree", "Invalid tree structure returned.")

        filtered_paths: list[str] = []
        for item in tree_items:
            if item.get("type") != "blob":
                continue
            path = item.get("path", "")
            if not path:
                continue
            segments = [s for s in path.split("/") if s]
            # segments count includes filename; depth 0 is root files (1 segment),
            # depth 1 is folder/file (2 segments). Max depth 3 allows up to 4 segments (dir1/dir2/dir3/file).
            if len(segments) > FILE_TREE_MAX_DEPTH + 1:
                continue
            if any(s in IGNORED_TREE_NAMES for s in segments):
                continue
            filtered_paths.append(path)

        total_count = len(filtered_paths)
        if total_count > FILE_TREE_MAX_ENTRIES:
            omitted = total_count - FILE_TREE_MAX_ENTRIES
            displayed = filtered_paths[:FILE_TREE_MAX_ENTRIES]
            return "\n".join(displayed) + f"\n... [{omitted} entries omitted due to size cap]"

        return "\n".join(filtered_paths)

    def _get_file_content(self, repo: str, path: str) -> str:
        clean_path = path.strip()
        lower_path = clean_path.lower()
        if any(lower_path.endswith(ext) for ext in BINARY_EXTENSIONS):
            return self._format_error(
                "get_file_content",
                f"Refusing to read binary file '{clean_path}'. Supported types are plain text and source files only.",
            )

        repo_name = self._clean_repo_name(repo)
        endpoint_path = clean_path.lstrip("/")
        data, err = self._get(f"/repos/{self._username}/{repo_name}/contents/{endpoint_path}")
        if err:
            return self._format_error("get_file_content", err)

        if isinstance(data, list):
            return self._format_error(
                "get_file_content",
                f"Path '{clean_path}' is a directory, not a file. Use 'get_file_tree' to list directory contents.",
            )

        if not isinstance(data, dict):
            return self._format_error("get_file_content", "Invalid content response format.")

        content_b64 = data.get("content", "")
        encoding = data.get("encoding", "")
        decoded, decode_err = self._decode_base64_content(content_b64, encoding, "get_file_content")
        if decode_err:
            return decode_err
        if decoded is None:
            decoded = ""

        if len(decoded) > FILE_CONTENT_CHAR_CAP:
            return (
                decoded[:FILE_CONTENT_CHAR_CAP]
                + f"\n\n[Truncated: File content exceeds {FILE_CONTENT_CHAR_CAP} characters]"
            )
        return decoded

    def _get_languages(self, repo: str) -> str:
        repo_name = self._clean_repo_name(repo)
        data, err = self._get(f"/repos/{self._username}/{repo_name}/languages")
        if err or not isinstance(data, dict):
            return self._format_error("get_languages", err or "Languages not found.")
        return json.dumps(data, indent=2)

    def _get_profile(self) -> str:
        data, err = self._get(f"/users/{self._username}")
        if err or not isinstance(data, dict):
            return self._format_error("get_profile", err or "Profile not found.")

        shaped = {
            "login": data.get("login"),
            "name": data.get("name"),
            "avatar_url": data.get("avatar_url"),
            "html_url": data.get("html_url"),
            "blog": data.get("blog"),
            "twitter_username": data.get("twitter_username"),
            "bio": data.get("bio"),
            "company": data.get("company"),
            "location": data.get("location"),
            "public_repos": data.get("public_repos"),
            "followers": data.get("followers"),
            "following": data.get("following"),
        }
        return json.dumps(shaped, indent=2)

    def _get_pinned_repos(self) -> str:
        query = """
        query($username: String!) {
          user(login: $username) {
            pinnedItems(first: 6, types: [REPOSITORY]) {
              nodes {
                ... on Repository {
                  name
                  description
                  isPrivate
                  stargazerCount
                  primaryLanguage {
                    name
                  }
                }
              }
            }
          }
        }
        """
        try:
            response = self._client.post(
                "https://api.github.com/graphql",
                json={"query": query, "variables": {"username": self._username}},
            )
            if response.status_code != 200:
                return self._format_error(
                    "get_pinned_repos",
                    f"GraphQL returned HTTP {response.status_code}.",
                )

            body = response.json()
            if "errors" in body and not body.get("data"):
                return self._format_error(
                    "get_pinned_repos",
                    f"GraphQL errors: {body['errors']}",
                )

            user_data = body.get("data", {}).get("user")
            if not user_data:
                return self._format_error(
                    "get_pinned_repos",
                    f"User '{self._username}' not found in GraphQL response.",
                )

            nodes = user_data.get("pinnedItems", {}).get("nodes", [])
            shaped: list[dict[str, Any]] = []
            for node in nodes:
                if node.get("isPrivate"):
                    continue
                lang = node.get("primaryLanguage")
                lang_name = lang.get("name") if isinstance(lang, dict) else None
                shaped.append(
                    {
                        "name": node.get("name"),
                        "description": node.get("description"),
                        "language": lang_name,
                        "stars": node.get("stargazerCount", 0),
                    }
                )
            return json.dumps(shaped, indent=2)
        except Exception as exc:
            logger.warning("GraphQL query failed for pinned repos: %s", exc)
            return self._format_error("get_pinned_repos", f"GraphQL request error: {exc}")

    def _get_recent_activity(self) -> str:
        data, err = self._get(
            f"/users/{self._username}/events/public",
            params={"per_page": 30},
        )
        if err or not isinstance(data, list):
            return self._format_error("get_recent_activity", err or "Activity not found.")

        shaped: list[dict[str, Any]] = []
        for item in data:
            repo_obj = item.get("repo", {})
            repo_name = repo_obj.get("name") if isinstance(repo_obj, dict) else str(repo_obj)
            # Filter strictly to the configured user's repositories
            if repo_name and not repo_name.lower().startswith(f"{self._username}/"):
                continue

            shaped.append(
                {
                    "type": item.get("type"),
                    "repo": repo_name,
                    "created_at": item.get("created_at"),
                }
            )
            if len(shaped) >= RECENT_ACTIVITY_CAP:
                break

        return json.dumps(shaped, indent=2)

    # ----------------------------------------------------------------------- #
    # Public Entry Point                                                      #
    # ----------------------------------------------------------------------- #

    def execute(self, raw_args: str | dict[str, Any]) -> str:
        """Execute a GitHub lookup action with guaranteed string return.

        Never raises an exception — all errors are formatted and returned
        as informative strings for the LLM to process conversationally.
        """
        try:
            # 1. Parse raw arguments
            if isinstance(raw_args, str):
                try:
                    parsed_dict = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    return f"[Tool error] Invalid JSON arguments: {exc}"
            elif isinstance(raw_args, dict):
                parsed_dict = raw_args
            else:
                return (
                    f"[Tool error] Arguments must be a JSON string or dict, "
                    f"got: {type(raw_args).__name__}"
                )

            # 2. Validate with Pydantic model
            try:
                args = GithubLookupArgs.model_validate(parsed_dict)
            except (ValidationError, ValueError) as exc:
                if isinstance(exc, ValidationError):
                    errors = [
                        f"{e['loc'][-1] if e['loc'] else 'args'}: {e['msg']}"
                        for e in exc.errors()
                    ]
                    return f"[Tool error] Validation failed: {'; '.join(errors)}"
                return f"[Tool error] Validation failed: {exc}"

            # 3. Guard repository ownership if repo is specified
            if args.repo:
                if not self._is_own_repo(args.repo):
                    return (
                        f"[Tool error] Access denied: Repository '{args.repo}' "
                        f"does not belong to user '{self._username}'."
                    )

            # 4. Dispatch to private action handler
            match args.action:
                case "list_repos":
                    return self._list_repos()
                case "get_repo_details":
                    if not args.repo:
                        return self._format_error("get_repo_details", "Repository name is required.")
                    return self._get_repo_details(args.repo)
                case "get_readme":
                    if not args.repo:
                        return self._format_error("get_readme", "Repository name is required.")
                    return self._get_readme(args.repo)
                case "get_file_tree":
                    if not args.repo:
                        return self._format_error("get_file_tree", "Repository name is required.")
                    return self._get_file_tree(args.repo)
                case "get_file_content":
                    if not args.repo or not args.path:
                        return self._format_error(
                            "get_file_content",
                            "Both 'repo' and 'path' parameters are required.",
                        )
                    return self._get_file_content(args.repo, args.path)
                case "get_languages":
                    if not args.repo:
                        return self._format_error("get_languages", "Repository name is required.")
                    return self._get_languages(args.repo)
                case "get_profile":
                    return self._get_profile()
                case "get_pinned_repos":
                    return self._get_pinned_repos()
                case "get_recent_activity":
                    return self._get_recent_activity()
                case _:
                    return f"[Tool error] Unknown action: '{args.action}'"

        except Exception as exc:
            logger.error("Unexpected error in GithubTool.execute: %s", exc, exc_info=True)
            return f"[Tool error] An unexpected error occurred: {exc}"
