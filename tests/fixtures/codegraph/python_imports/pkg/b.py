from .a import known as local_known, missing
from pkg.a import known
import pkg.a as module_a
from external.package import foreign
from pkg.a import *  # noqa: F401,F403


def use():
    known()
    local_known()
    module_a.known()
    missing()
    foreign()
