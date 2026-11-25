import traceback

import pytest

from iwiki_mcp.codegraph.signature_privacy import project_python_signature


@pytest.mark.parametrize(("stored", "display"), [
    ('function|(token:str="MiXeD")->list["User"]', 'function|(token:str=...)->list[User]'),
    ('method|(self,x:Unknown[User,"MiXeD"])', 'method|(self,x:…)'),
    ('async_function|async(x:Annotated[Literal["MiXeD"],meta()])',
     'async_function|async(x:Literal[…])'),
    ('method|async(self,x,/,y=None,*args:str,flag:bool=False,**kw:object)->pkg.Model|None',
     'method|async(self,x,/,y=...,*args:str,flag:bool=...,**kw:object)->pkg.Model|None'),
    ('function|(x,*,flag,mode:typing.Literal["A","B"])->typing.Annotated[list[int],"hidden"]',
     'function|(x,*,flag,mode:Literal[…])->list[int]'),
    ("function|(x:\"dict[str,list[Literal['MiXeD']]]\")->make_type()",
     'function|(x:dict[str,list[Literal[…]]])->…'),
    ('function|(x:pkg.Generic["hidden"])->other.List["hidden"]', 'function|(x:…)->…'),
    ('function|(x:list[A,"hidden"])->list[make()]', 'function|(x:…)->list[…]'),
    ('function|(*args,**kwargs)->tuple[str,...]', 'function|(*args,**kwargs)->tuple[str,...]'),
    ('function|(x:typing.Callable[[A,"B"],C])', 'function|(x:typing.Callable[[A,B],C])'),
    ('(x="MiXeD")', '(x=...)'),
    # PEP 701 permits reusing quotes inside an f-string (Python 3.12+).
    ('function|(x=f"{row["MiXeD"]}")', 'function|(x=...)'),
])
def test_projects_only_recognized_type_positions(stored, display):
    assert project_python_signature(stored) == display


@pytest.mark.parametrize("stored", [
    'function|(x="MiXeD"',
    'MiXeD|(x)',
    'function|(x): pass\ndef extra(MiXeD)',
    'method|(x)->int # MiXeD',
])
def test_rejects_malformed_declarations_without_source(stored):
    with pytest.raises(ValueError) as caught:
        project_python_signature(stored)
    assert "MiXeD" not in "".join(traceback.format_exception(caught.value))


def test_does_not_execute_annotation_or_default(tmp_path):
    target = tmp_path / "side-effect"
    call = f'open({str(target)!r},"w")'
    assert project_python_signature(f'function|(x:{call}={call})') == 'function|(x:…=...)'
    assert not target.exists()
