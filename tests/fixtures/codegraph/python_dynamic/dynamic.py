def local():
    return None


def use(factory, container):
    local()
    factory.make()
    getattr(factory, "make")()
    container.resolve("service")()
    external.call()  # noqa: F821
