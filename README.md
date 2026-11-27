# databunkerpro-llm-poc

Keep LLM conversations free of PII, across turns and restarts.

[newmean-pii](https://github.com/newmean-ai/pii) finds personal data in text and replaces it with numbered
placeholders (`[PERSON_1]`). Its **vault** remembers which placeholder stands for which value, so the real values can
be put back into the model's answer. The vault lives in memory; newmean-pii leaves storing it to you.

This PoC stores it in **Databunker Pro**: every value becomes a Databunker **token**, and each conversation session
keeps a small index file that maps its placeholders to those tokens. The LLM sees only placeholders, the app's storage
sees only placeholders and token uuids, and the real values exist only inside Databunker Pro.

No changes to newmean-pii or Databunker Pro are needed: it uses newmean-pii's public `Vault(state)` /
`vault.to_json()` and Databunker Pro's token and file APIs.

## Architecture

```
                      ┌──────────────────── your app ────────────────────┐
 "Call Kim Minsu"  ─► │ ① load   session file → tokens → values → Vault   │
                      │ ② mask   newmean-pii: "Call [PERSON_1]"  ─────────┼──► LLM provider
                      │ ④ restore "I called [PERSON_1]" → "...Kim Minsu"  │◄── ③ answer
                      │ ⑤ save   new values → tokens; write session file  │
                      └───────────────┬───────────────────────────────────┘
                                      │
                      ┌──────── Databunker Pro ─────────────────────────────┐
                      │ tokens        7f3a2c1e-… → "Kim Minsu"  (encrypted) │
                      │               91bc44d0-… → "010-1234-5678"          │
                      │ session file  vault-<app>-<sha256(session)>.json    │
                      │               [PERSON_1] → 7f3a2c1e-…  (no PII)     │
                      └─────────────────────────────────────────────────────┘
```

### From `[PERSON_1]` to a token

**Turn 1, new session.** `load()` finds no session file and returns an empty vault. newmean-pii masks the message:

```
"Call Kim Minsu on 010-1234-5678"  →  "Call [PERSON_1] on [PHONE_1]"
vault (memory):  [PERSON_1] = "Kim Minsu",  [PHONE_1] = "010-1234-5678"
```

`save()` sends the values the session has not tokenized yet in one `TokenCreateBulk` call (`unique: true`,
`slidingtime: 30d`) and gets a token uuid for each:

```
"Kim Minsu"      → 7f3a2c1e-8872-d109-780b-5bcadb96ebd0
"010-1234-5678"  → 91bc44d0-4aeb-231c-daaf-2efd7bd2ae71
```

Then it writes the session file, encrypted by Databunker Pro like any file, though it holds no PII anyway:

```json
{
  "format": 2,
  "newmean": "1.7.2",
  "session": "<sha256 of the session id>",
  "entries": [
    { "tag": "[PERSON_1]", "label": "PERSON", "token": "7f3a2c1e-8872-d109-780b-5bcadb96ebd0" },
    { "tag": "[PHONE_1]",  "label": "PHONE",  "token": "91bc44d0-4aeb-231c-daaf-2efd7bd2ae71" }
  ],
  "counters": { "PERSON": 1, "PHONE": 1 },
  "local": []
}
```

**Turn 2, maybe on another server.** `load()` reads the session file, resolves all its tokens in one call
(`BulkListUnlock` + `BulkListTokens`) and rebuilds the vault: `[PERSON_1]` is "Kim Minsu" again. When the user writes
"Kim Minsu" again, newmean-pii reuses `[PERSON_1]`. Only values new in this turn are sent to `TokenCreateBulk`.

Two ids, two jobs:

| | `[PERSON_1]` | `7f3a2c1e-…` |
|---|---|---|
| made by | newmean-pii, numbered per session | Databunker Pro |
| seen by | the LLM | your app (in the session file) and Databunker only |
| why | short (about 4 LLM tokens), easy for a model to copy, means nothing outside its session | stable identity for the value; resolves to the value only through Databunker |

The token uuid never reaches the LLM, so the provider cannot link the same person across sessions.

## Expiration

Two things expire, both with a **sliding** window (`slidingtime`, default `30d`):

- **Tokens.** A token's window restarts whenever it is used: when `load()` reads it (every turn of every session
  that holds it), and when `TokenCreateBulk` gets its value again (`unique` returns the existing token and resets its
  countdown). So a value stays alive as long as some conversation keeps using it, and disappears about 30 days
  after the last use.
- **Session files.** Every `save()` writes a new file, so a session's window restarts on every turn that changes it.
  `file_slidingtime` sets it apart from the tokens' window.

When a token has expired, `load()` leaves its placeholder out of the vault and lists it in `store.expired`. The
session's counters are kept, so `[PERSON_1]` is never given to a different value later; a new value gets the next
number. If the LLM still writes `[PERSON_1]`, it stays as written in the answer.

## The same value in different sessions

`unique: true` makes Databunker Pro keep **one token per value**:

```
session A:  [PERSON_1] → 7f3a2c1e-…  ┐
                                     ├─ the same token, "Kim Minsu"
session B:  [PERSON_3] → 7f3a2c1e-…  ┘
```

- **Placeholders are per session.** "Kim Minsu" can be `[PERSON_1]` in A and `[PERSON_3]` in B; the session file
  maps each to the same token.
- **One record, not one per session.** The value is stored once, which also keeps the record count down.
- **Shared life.** Either session using the token extends it for both.
- **Deleting a session never deletes tokens.** `store.delete()` removes only the session file, because another
  session may still hold the same token. Unused tokens expire on their own.

## Duplicates

| Duplicate | What happens |
|---|---|
| A value repeated in the same session | newmean-pii keeps one placeholder for it, also in other spellings (`KIM MINSU`, full-width, NFD) |
| A value the session already tokenized | not sent to `TokenCreateBulk` again |
| A value another session tokenized | `TokenCreateBulk` returns the existing token (`duplicates`) and restarts its expiry |
| Identical session file content | Databunker Pro dedupes file content per user and returns the existing file. The session hash in the file keeps two sessions apart; an unchanged vault is not written again, and the store never deletes a file uuid that came back unchanged |

## Quickstart

```bash
docker run -p 3000:3000 -d --rm --name databunkerpro securitybunker/databunkerpro demo

cd python
pip install -e ".[example,test]"

python examples/chat.py                # fake LLM, no API key
python examples/chat.py --llm claude   # Claude (ANTHROPIC_API_KEY)
pytest -q                              # integration tests against the demo instance
```

`DATABUNKER_URL` (default `http://localhost:3000`) and `DATABUNKER_TOKEN` (default `DEMO`) point it at another
instance.

## Usage

```python
from newmean_pii import create_redactor
from databunker_vault import DatabunkerClient, DatabunkerVaultStore

client = DatabunkerClient()
redactor = create_redactor()

store = DatabunkerVaultStore(client, session_id="chat-42", app="support-bot")
vault = store.load()                                   # this session's vault, or an empty one
masked = redactor.redact(user_text, vault=vault).text  # send `masked` to the LLM
answer = vault.restore(llm_answer)                     # show `answer` to the user
store.save(vault)
...
store.delete()                                         # when the conversation is closed
```

## Identities

- **Service user.** Files in Databunker Pro belong to a user, so each `app` gets one service user (login
  `pii-vault-<app>`) that owns its session files. It is created on first use and its token is cached in memory.
- **File name.** `vault-<app>-<sha256(session_id)>.json`: any session id fits, and the id itself is not kept in the
  file metadata.
- **Permissions.** Resolving tokens uses `BulkListUnlock` + `BulkListTokens`, which needs bulk-read access.

## Limits

- **Records.** Each distinct value is one Databunker record (tokens count toward the license); session files do not.
- **Erasure by person.** Tokens are not linked to the person they describe. A "forget me" request has to find that
  person's tokens (a unique `TokenCreateBulk` of the value returns its uuid) and `TokenDelete` them; the sessions
  that held them then find them expired.
- **Each save is create + delete.** Databunker Pro has no file update call. A crash between the two leaves an extra
  copy; `load` reads the newest, and `delete()` removes every copy.
- **One writer per session.** Two processes saving the same session at once: the last save wins.
- **Replays are not kept.** newmean-pii's record of the model's own answers is keyed by restored text (PII), so it is
  left out. The app should resend the model's earlier answers as the model wrote them, as `examples/chat.py` does.
- **Token type.** Every value is a `string` token; card numbers are not format-preserving `creditcard` tokens.
- **Detection is not perfect.** What newmean-pii does not find reaches the LLM as written.

## Layout

```
python/databunker_vault/client.py   Databunker Pro HTTP calls (users, tokens, files)
python/databunker_vault/store.py    DatabunkerVaultStore: load / save / delete
python/examples/chat.py             three turns, each starting from a fresh process state
python/tests/test_store.py          integration tests
```

## License

MIT
