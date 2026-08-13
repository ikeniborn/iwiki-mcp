from .a import known as local_known, missing
from pkg.a import known
import pkg.a as module_a
from external.package import foreign
from pkg.a import *  # noqa: F401,F403
import pkg.a as svc
from pkg.a import helper
import external.pkg  # noqa: F401


def use():
    known()
    local_known()
    module_a.known()
    missing()
    foreign()
    svc.helper()
    helper()
