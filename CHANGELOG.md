# Changelog

## Unreleased

### Added

- **`/sonar:pulse` looks for the thing you know is in there somewhere.** Fire it
  mid-conversation (`/sonar:pulse that note about the retry budget`) and the
  best matches come back in a few lines, as `path:line`. It searches the files
  the session has already loaded first, then the current repo, and a match in
  a file already in context ranks above an equal one elsewhere. A deeper search
  keeps going in the background, through git history, your past Claude Code
  sessions in this project, and sibling repos, and what it finds arrives with
  your next prompt without you asking again.
- **The deeper search follows where the matches lead, up to two hops.** It
  follows links, imports, and paths written in the matched files, and adds a
  few topic words that keep showing up next to your terms. Each result says how
  it was reached, such as "2 hops from README.md through docs/a.md via
  'retry budget'".
- **Results are pointers with short snippets.** Each snippet is capped at 160
  characters, and anything shaped like a token, key, or password is replaced
  with `[redacted]`. Past sessions are searched for what was said, not for tool
  output, and from the current session sonar reads only which files were
  loaded. sonar never moves, rewrites, or fetches what it finds.
