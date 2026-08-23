import pytest

from iwiki_mcp.telegram_bot.access import AccessPolicy
from iwiki_mcp.telegram_bot.conversation import ConversationService
from iwiki_mcp.telegram_bot.iwiki import RemoteIwikiError


class FakeRemote:
    def __init__(self):
        self.write_calls = []
        self.update_calls = []
        self.update_error = None
        self.revision = 7
        self.section_hash = "fresh-hash"

    async def list_domains(self):
        return ["team"]

    async def search(self, domain, query):
        return []

    async def read_page(self, domain, slug, heading=None):
        return {
            "domain": domain,
            "slug": slug,
            "heading": heading,
            "body": "Existing body",
            "revision": self.revision,
            "section_hash": self.section_hash,
        }

    async def write_page(self, domain, slug, markdown):
        self.write_calls.append((domain, slug, markdown))

    async def update_section(
        self, domain, slug, heading, new_body, revision, section_hash
    ):
        self.update_calls.append(
            (domain, slug, heading, new_body, revision, section_hash)
        )
        if self.update_error:
            raise RemoteIwikiError(self.update_error)


class FakeInference:
    async def draft_markdown(self, request, context):
        return "# Draft\n\nRequested change"


@pytest.fixture
def clock():
    value = [100.0]

    def now():
        return value[0]

    now.value = value
    return now


@pytest.fixture
async def service(clock):
    remote = FakeRemote()
    value = ConversationService(
        AccessPolicy(frozenset({1001, 2002})),
        remote,
        FakeInference(),
        confirmation_ttl_seconds=60,
        clock=clock,
    )
    value.remote = remote
    await value.select_domain(1001, "team")
    await value.select_domain(2002, "team")
    return value


@pytest.mark.asyncio
async def test_write_requires_confirmation(service):
    preview = await service.propose_create(1001, "runbook", "Add a deploy runbook")

    assert preview.buttons == ("confirm", "reject")
    assert preview.text.startswith("# Draft")
    assert service.remote.write_calls == []


@pytest.mark.asyncio
async def test_confirmed_create_writes_once(service):
    token = (
        await service.propose_create(1001, "runbook", "Add a deploy runbook")
    ).token

    reply = await service.confirm_write(1001, token)

    assert reply.text == "Page change saved."
    assert service.remote.write_calls == [
        ("team", "runbook", "# Draft\n\nRequested change")
    ]


@pytest.mark.asyncio
async def test_reject_consumes_pending_write(service):
    token = (
        await service.propose_create(1001, "runbook", "Add a deploy runbook")
    ).token

    assert (await service.reject_write(1001, token)).text == "Change rejected."
    assert (await service.confirm_write(1001, token)).text == "Confirmation is invalid."
    assert service.remote.write_calls == []


@pytest.mark.asyncio
async def test_confirmation_is_bound_to_telegram_id(service):
    token = (
        await service.propose_create(1001, "runbook", "Add a deploy runbook")
    ).token

    reply = await service.confirm_write(2002, token)

    assert reply.text == "Confirmation is invalid."
    assert service.remote.write_calls == []


@pytest.mark.asyncio
async def test_expired_confirmation_is_destroyed(service, clock):
    token = (
        await service.propose_create(1001, "runbook", "Add a deploy runbook")
    ).token
    clock.value[0] = 161.0

    reply = await service.confirm_write(1001, token)

    assert reply.text == "Confirmation expired."
    assert service.remote.write_calls == []


@pytest.mark.asyncio
async def test_confirmed_update_uses_fresh_revision_and_section_hash(service):
    token = (
        await service.propose_update(
            1001, "guide/deploy", "Steps", "replace step two"
        )
    ).token
    service.remote.revision = 8
    service.remote.section_hash = "new-hash"

    reply = await service.confirm_write(1001, token)

    assert reply.text == "Page change saved."
    assert service.remote.update_calls == [
        (
            "team",
            "guide/deploy",
            "Steps",
            "# Draft\n\nRequested change",
            8,
            "new-hash",
        )
    ]


@pytest.mark.asyncio
async def test_update_conflict_is_not_retried(service):
    token = (
        await service.propose_update(
            1001, "guide/deploy", "Steps", "replace step two"
        )
    ).token
    service.remote.update_error = "section_conflict"

    reply = await service.confirm_write(1001, token)

    assert reply.text == "Page changed; request a new preview."
    assert len(service.remote.update_calls) == 1
