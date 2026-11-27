"""Integration tests against a Databunker Pro instance (demo mode: docker run ... databunkerpro demo)."""
import json
import time
import uuid

import pytest
from newmean_pii import Vault, create_redactor

from databunker_vault import DatabunkerClient, DatabunkerError, DatabunkerVaultStore, VaultFormatError, service_user_token
from databunker_vault import store as store_mod


@pytest.fixture(scope="session")
def client():
    c = DatabunkerClient()
    try:
        c.user_get("login", "pii-vault-probe")
    except DatabunkerError as e:
        if e.status != 404:
            pytest.skip(f"Databunker not usable: {e}")
    except OSError as e:
        pytest.skip(f"Databunker not reachable at {c.url}: {e}")
    return c


@pytest.fixture
def app():
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def name():
    """A value unique to the test, so tokens from other runs never interfere."""
    return f"Person {uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="session")
def redactor():
    return create_redactor(size="medium")


def files_named(client, store):
    return [f for f in client.file_list(store.user_token) if f["filename"] == store.filename]


def session_file(client, store):
    return json.loads(client.file_get(store.user_token, store.filename)["filedata"])


def test_service_user_created_then_reused(client, app):
    login = f"pii-vault-{app}"
    token = service_user_token(client, login)
    store_mod._users.clear()
    assert service_user_token(client, login) == token
    assert client.user_get("token", token)["profile"]["login"] == login


def test_service_user_create_race():
    class RacingClient:
        url = "fake"

        def __init__(self):
            self.gets = 0

        def user_get(self, mode, identity):
            self.gets += 1
            if self.gets == 1:
                raise DatabunkerError(404, "The user record was not found")
            return {"token": "tok-1"}

        def user_create(self, profile):
            raise DatabunkerError(409, "Duplicate index: login")

    store_mod._users.clear()
    assert service_user_token(RacingClient(), "pii-vault-race") == "tok-1"


def test_new_session_loads_empty_vault(client, app):
    vault = DatabunkerVaultStore(client, "s1", app=app).load()
    assert len(vault) == 0


def test_round_trip_with_redactor(client, app, redactor):
    store = DatabunkerVaultStore(client, "chat-1", app=app)
    vault = store.load()
    masked = redactor.redact("Call Kim Minsu on 010-1234-5678.", vault=vault).text
    assert "Kim Minsu" not in masked and "[PERSON_1]" in masked
    store.save(vault)

    # a fresh store and vault, as after a restart
    reloaded = DatabunkerVaultStore(client, "chat-1", app=app).load()
    assert reloaded.entries() == vault.entries()
    assert redactor.redact("Has Kim Minsu replied yet?", vault=reloaded).text == "Has [PERSON_1] replied yet?"
    assert reloaded.restore("I called [PERSON_1] on [PHONE_1].") == "I called Kim Minsu on 010-1234-5678."


def test_session_file_holds_tokens_not_values(client, app, name):
    store = DatabunkerVaultStore(client, "s", app=app)
    vault = store.load()
    vault.tag("PERSON", name)
    store.save(vault)
    raw = client.file_get(store.user_token, store.filename)["filedata"].decode()
    assert name not in raw
    (entry,) = json.loads(raw)["entries"]
    assert entry["tag"] == "[PERSON_1]"
    assert client.tokens_get([entry["token"]]) == {entry["token"]: name}


def test_same_value_in_two_sessions_shares_one_token(client, app, name):
    a = DatabunkerVaultStore(client, "a", app=app)
    va = a.load()
    va.tag("PERSON", name)
    a.save(va)

    b = DatabunkerVaultStore(client, "b", app=app)
    vb = b.load()
    vb.tag("PERSON", f"Other {name}")
    assert vb.tag("PERSON", name) == "[PERSON_2]"  # placeholders are per session
    b.save(vb)

    token_a = session_file(client, a)["entries"][0]["token"]
    token_b = {e["tag"]: e["token"] for e in session_file(client, b)["entries"]}["[PERSON_2]"]
    assert token_a == token_b

    # closing one session leaves the shared token to the other
    a.delete()
    assert DatabunkerVaultStore(client, "b", app=app).load().restore("[PERSON_2]") == name


def test_save_replaces_previous_file(client, app, name):
    store = DatabunkerVaultStore(client, "s", app=app)
    vault = store.load()
    vault.tag("PERSON", name)
    store.save(vault)
    vault.tag("EMAIL", f"{uuid.uuid4().hex[:6]}@example.com")
    store.save(vault)
    assert len(files_named(client, store)) == 1
    assert len(DatabunkerVaultStore(client, "s", app=app).load()) == 2


def test_unchanged_vault_is_not_rewritten_or_lost(client, app, name):
    store = DatabunkerVaultStore(client, "s", app=app)
    vault = store.load()
    vault.tag("PERSON", name)
    store.save(vault)
    first = files_named(client, store)[0]["fileuuid"]

    # reload in a new store and save without changes: Databunker would return the same file
    again = DatabunkerVaultStore(client, "s", app=app)
    v2 = again.load()
    again.save(v2)
    v2.tag("PHONE", f"+1 415-555-{uuid.uuid4().int % 10000:04d}")
    again.save(v2)
    again.save(v2)
    names = files_named(client, store)
    assert len(names) == 1 and names[0]["fileuuid"] != first
    assert len(DatabunkerVaultStore(client, "s", app=app).load()) == 2


def test_two_empty_sessions_do_not_collide(client, app):
    a, b = DatabunkerVaultStore(client, "a", app=app), DatabunkerVaultStore(client, "b", app=app)
    a.save(Vault())
    b.save(Vault())
    assert len(files_named(client, a)) == 1 and len(files_named(client, b)) == 1
    assert len(a.load()) == 0 and len(b.load()) == 0


def test_file_of_other_session_is_rejected(client, app):
    store = DatabunkerVaultStore(client, "mine", app=app)
    doc = {"format": 2, "session": "someone-else", "entries": []}
    client.file_create(store.user_token, store.filename, json.dumps(doc).encode(), tags=["pii-vault"])
    with pytest.raises(VaultFormatError):
        store.load()


def test_load_extends_token_life(client, app, name):
    store = DatabunkerVaultStore(client, "s", app=app, slidingtime="3s", file_slidingtime="1h")
    vault = store.load()
    vault.tag("PERSON", name)
    store.save(vault)
    for _ in range(3):  # 6 s in all, past the 3 s window, but each load restarts it
        time.sleep(2)
        assert DatabunkerVaultStore(client, "s", app=app).load().restore("[PERSON_1]") == name


def test_expired_token_is_dropped_and_its_number_not_reused(client, app, name):
    store = DatabunkerVaultStore(client, "s", app=app, slidingtime="2s", file_slidingtime="1h")
    vault = store.load()
    vault.tag("PERSON", name)
    store.save(vault)
    time.sleep(4)
    later = DatabunkerVaultStore(client, "s", app=app)
    v = later.load()
    assert later.expired == ["[PERSON_1]"] and len(v) == 0
    assert v.tag("PERSON", f"New {name}") == "[PERSON_2]"


def test_delete_removes_session(client, app, name):
    store = DatabunkerVaultStore(client, "s", app=app)
    vault = store.load()
    vault.tag("PERSON", name)
    store.save(vault)
    assert store.delete() == 1
    assert len(DatabunkerVaultStore(client, "s", app=app).load()) == 0
