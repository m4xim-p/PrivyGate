import asyncio

from app.routing import RoundRobinRouter


def test_round_robin_cycles_through_backends() -> None:
    async def select() -> list[str]:
        router = RoundRobinRouter(["backend-1", "backend-2", "backend-3"])
        return [await router.next_backend() for _ in range(7)]

    assert asyncio.run(select()) == [
        "backend-1",
        "backend-2",
        "backend-3",
        "backend-1",
        "backend-2",
        "backend-3",
        "backend-1",
    ]
