# <img src="favicon.svg" alt="sonar" width="64" height="64" style="vertical-align: middle"> sonar

**Ping for what you're looking for.**

You know it's in there somewhere: a guide you read last week, a decision buried
in history, a note in another repo. sonar searches everything a session can
reach for it. Shallow matches come back right away, and sonar keeps listening
while you work: the files those lead to, sibling repos, git history, links, and
past transcripts. When your next turn starts, it hands over what else turned up,
along with anything it noticed on the way, such as a link to a local file that
isn't there.

## Send a ping

Mid-conversation, as an aside:

```text
/sonar:pulse that note about the retry budget
```

Claude picks the search terms from what you said and what the conversation is
about. sonar searches the files this session has already loaded first (the
ones Claude read or edited, and the ones you @-mentioned), then the current
repo and any paths you name. A match in a file that's already in context ranks
above an equal match elsewhere, since that's the likeliest place. The best
matches come back in a few lines, as `path:line`.

A deeper search keeps going in the background:

- files the matches lead to, up to two hops out: links, imports, and paths
  written in the text. sonar adds a few topic words, ones that keep showing up
  next to your terms in the matches but are rare in the rest of the repo, and
  each result says how it was reached, such as "2 hops from README.md through
  docs/a.md via 'retry budget'"
- git history: commit messages, and commits that added or removed the term
- your past Claude Code sessions in this project
- sibling repos in the same parent directory
- web links in the matched files that name the term (listed, never fetched)

When you send your next prompt, what it found so far arrives with it, and
Claude mentions the results that look like what you were after. If the search
is still going, the rest arrives with the prompt after that. Each result is
handed over once.

## What it shows, and what it doesn't

sonar only reads. It never moves, rewrites, or fetches what it finds.

Every result is a pointer (`path:line`, a commit, or a past session you can
reopen with `claude --resume`) with a snippet of at most 160 characters.
Snippets pass through a filter that replaces anything shaped like a token,
key, password, or credential in a URL with `[redacted]`, and withholds a line
that looks like a private key. Past sessions are searched for what you typed
and what Claude wrote; tool calls, tool output, and thinking are skipped. From
the current session, sonar reads only which files were loaded, never their
contents as they appeared there or any tool output. Links
are shown without their query strings.

Results are kept per session under Claude Code's plugin data directory and
removed after seven days.
