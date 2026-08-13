class Base:
    pass


class Child(Base):
    pass


class ExternalChild(external.Base):  # noqa: F821
    pass
