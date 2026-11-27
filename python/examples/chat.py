"""Multi-turn chat where the LLM never sees PII and the vault lives in Databunker Pro.

    python examples/chat.py              # fake LLM, no API key needed
    python examples/chat.py --llm claude # real Claude (ANTHROPIC_API_KEY or `ant auth login`)

Every turn starts from scratch (new store, new vault), as if the app had restarted, so the
placeholders survive only because the vault is loaded back from Databunker.
"""
import argparse
import os
import uuid

from newmean_pii import create_redactor, vault_tags

from databunker_vault import DatabunkerClient, DatabunkerVaultStore

SYSTEM = (
    "You are a helpful customer support assistant. "
    "Text in square brackets with a label and a number, such as [PERSON_n], stands for a value hidden from you. "
    "Copy it exactly as written, brackets included, wherever you refer to it."
)

TURNS = [
    "I'm handling a ticket from Kim Minsu. His phone is 010-1234-5678 and his email is kim.minsu@example.com. "
    "Draft a two-sentence callback note for the team.",
    "What number should I call, and which email should the confirmation go to?",
    "Write a one-line SMS to Kim Minsu confirming tomorrow's 10am call.",
]


def fake_llm(messages):
    tags = list(dict.fromkeys(vault_tags(messages[-1]["content"])))
    return f"Noted, I'll follow up with {', '.join(tags)}." if tags else "Noted."


def claude_llm(messages):
    import anthropic

    response = anthropic.Anthropic().beta.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        system=SYSTEM,
        messages=messages,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        return "(the model declined this request)"
    return "".join(b.text for b in response.content if b.type == "text")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--llm", choices=["fake", "claude"], default="fake")
    p.add_argument("--session", default=f"demo-{uuid.uuid4().hex[:8]}")
    p.add_argument("--keep", action="store_true", help="keep the vault file in Databunker afterwards")
    args = p.parse_args()
    llm = claude_llm if args.llm == "claude" else fake_llm

    client = DatabunkerClient()
    redactor = create_redactor(size="medium")
    # the app's own chat history; assistant turns also keep what the LLM wrote, which holds no PII
    history = []

    print(f"session {args.session}, LLM {args.llm}, Databunker {client.url}\n")
    for i, text in enumerate(TURNS, 1):
        store = DatabunkerVaultStore(client, args.session, app="chat-demo")
        vault = store.load()
        loaded = len(vault)
        history.append({"role": "user", "content": text})

        # user turns are masked again (known values keep their placeholders); assistant turns go back as written
        masked = [
            {"role": m["role"], "content": m["masked"] if m["role"] == "assistant" else redactor.redact(m["content"], vault=vault).text}
            for m in history
        ]
        answer_masked = llm(masked)
        answer = vault.restore(answer_masked)
        history.append({"role": "assistant", "content": answer, "masked": answer_masked})
        store.save(vault)

        print(f"── turn {i} (vault loaded from Databunker: {loaded} values, saved: {len(vault)}) ──")
        print(f"user      : {text}")
        print(f"LLM saw   : {masked[-1]['content']}")
        print(f"LLM said  : {answer_masked}")
        print(f"user sees : {answer}\n")

    print(f"Databunker file: {store.filename}")
    if not args.keep:
        store.delete()
        print("(deleted; pass --keep to leave it)")


if __name__ == "__main__":
    main()
