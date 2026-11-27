"""Prints every stage of the pipeline: original text, secured text, LLM answer, index file and Databunker tokens.

    python examples/pipeline.py              # fake LLM, no API key needed
    python examples/pipeline.py --llm claude # real Claude
"""
import argparse
import json
import time
import uuid

from chat import claude_llm, fake_llm
from newmean_pii import create_redactor

from databunker_vault import DatabunkerClient, DatabunkerVaultStore

TURNS = [
    "Please call Kim Minsu on 010-1234-5678 and email kim.minsu@example.com about order 4471.",
    "Also send the invoice to Kim Minsu's colleague Sarah Chen at sarah.chen@example.com.",
]


def section(title):
    print(f"\n{'─' * 4} {title} {'─' * max(0, 70 - len(title))}")


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def show_tokens(client, entries, started):
    unlock = client.api.bulk_list_unlock()["unlockuuid"]
    rows = {r["tokenuuid"]: r for r in client.api.bulk_list_tokens(unlock, [e["token"] for e in entries]).get("rows") or []}
    for e in entries:
        r = rows.get(e["token"])
        if r is None:
            print(f"  {e['tag']:<12} {e['token']}  (expired)")
            continue
        reused = "  ← existed before this run (shared token)" if r.get("creationtime", 0) < started else ""
        print(f"  {e['tag']:<12} {r['tokenuuid']}  {r['record']!r}{reused}")
        print(f"  {'':<12} sliding {r.get('slidingtime', 0) // 86400} days, "
              f"created {when(r.get('creationtime'))}, last used {when(r.get('lastusedtime'))}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--llm", choices=["fake", "claude"], default="fake")
    p.add_argument("--session", default=f"pipeline-{uuid.uuid4().hex[:8]}")
    p.add_argument("--keep", action="store_true", help="keep the index file in Databunker afterwards")
    args = p.parse_args()
    llm = claude_llm if args.llm == "claude" else fake_llm

    client = DatabunkerClient()
    redactor = create_redactor(size="medium")
    history = []
    started = int(time.time())
    print(f"session {args.session}, LLM {args.llm}, Databunker {client.url}")

    for i, text in enumerate(TURNS, 1):
        print(f"\n{'═' * 78}\nTURN {i}")
        store = DatabunkerVaultStore(client, args.session, app="pipeline-demo")
        vault = store.load()

        section("1. original text")
        print(text)

        result = redactor.redact(text, vault=vault)
        section("2. secured text (sent to the LLM)")
        for s in result.spans:
            print(f"  found {s.label:<10} {text[s.start:s.end]!r}  ({s.source})")
        print(result.text)

        history.append({"role": "user", "content": text, "masked": result.text})
        answer_masked = llm([{"role": m["role"], "content": m["masked"]} for m in history])
        answer = vault.restore(answer_masked)
        history.append({"role": "assistant", "content": answer, "masked": answer_masked})
        section("3. LLM answer")
        print(f"  raw      : {answer_masked}")
        print(f"  restored : {answer}")

        store.save(vault)
        index = json.loads(client.file_get(store.user_token, store.filename)["filedata"])
        section(f"4. index file in Databunker ({store.filename[:40]}...)")
        print(json.dumps(index, indent=2))

        section("5. tokens in Databunker")
        show_tokens(client, index["entries"], started)

    if not args.keep:
        store.delete()
        print("\n(index file deleted; tokens stay and expire on their own; pass --keep to keep the file)")


if __name__ == "__main__":
    main()
