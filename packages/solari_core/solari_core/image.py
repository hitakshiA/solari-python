"""Declarative Image builder — mirrors the TypeScript ``src/image.ts``.

Fluent chain that compiles to a structured ``steps[]`` (typed ops the snapshot
exec-orchestrator runs directly) plus a byte-identical ``Dockerfile.fragment``
(the Docker rebuild path) + an apt ``packages`` list + local files. ``compile()``
is pure + offline (unit-tested); actually BUILDING a template
(``pt.templates.build``) needs the gateway ``/templates`` backend, which is
Phases 2-6 in docs/TEMPLATE-BUILD-PIPELINE.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

ImageKind = Literal["sandbox", "desktop"]

# A structured build op. Mirrors the TS ``ImageStep`` union; the snapshot-based
# exec orchestrator (approach A) runs these directly, while the Docker path
# (approach B) renders them to a ``Dockerfile.fragment``. Kept as plain dicts so
# they serialize to the identical wire shape as the TS SDK.
ImageStep = Dict[str, Any]


@dataclass
class LocalCopy:
    src: str
    dest: str


@dataclass
class CompiledImage:
    kind: ImageKind
    packages: List[str]
    fragment: str
    steps: List[ImageStep] = field(default_factory=list)
    localFiles: List[LocalCopy] = field(default_factory=list)
    base: Optional[str] = None
    fromTemplate: Optional[str] = None


def _env_value(v: str) -> str:
    # Mirror the TS JSON.stringify-when-whitespace behaviour.
    return json.dumps(v) if any(c.isspace() for c in v) else v


def _render_dockerfile(step: ImageStep) -> str:
    """Render one structured step to its Dockerfile line (approach B)."""
    op = step["op"]
    if op == "pip":
        return "RUN pip3 install --no-cache-dir " + " ".join(step["packages"])
    if op == "run":
        return "RUN " + step["command"]
    if op == "env":
        return f"ENV {step['key']}={_env_value(step['value'])}"
    if op == "workdir":
        return "WORKDIR " + step["dir"]
    if op == "entrypoint":
        return "ENTRYPOINT " + json.dumps(step["command"])
    if op == "cmd":
        return "CMD " + json.dumps(step["command"])
    raise ValueError(f"unknown image step op: {op}")


class Image:
    def __init__(self) -> None:
        self._kind: ImageKind = "sandbox"
        self._base: Optional[str] = None
        self._from_template: Optional[str] = None
        self._packages: List[str] = []
        self._steps: List[ImageStep] = []
        self._local_files: List[LocalCopy] = []

    @classmethod
    def base(cls, image: str) -> "Image":
        i = cls()
        i._base = image
        return i

    @classmethod
    def from_template(cls, name: str) -> "Image":
        i = cls()
        i._from_template = name
        return i

    def kind(self, k: ImageKind) -> "Image":
        self._kind = k
        return self

    def apt_install(self, pkgs: List[str]) -> "Image":
        self._packages.extend(pkgs)
        return self

    def pip_install(self, pkgs: List[str]) -> "Image":
        if pkgs:
            self._steps.append({"op": "pip", "packages": list(pkgs)})
        return self

    def run_commands(self, *cmds: str) -> "Image":
        for c in cmds:
            self._steps.append({"op": "run", "command": c})
        return self

    def env(self, vars: Dict[str, str]) -> "Image":
        for k, v in vars.items():
            self._steps.append({"op": "env", "key": k, "value": v})
        return self

    def workdir(self, d: str) -> "Image":
        self._steps.append({"op": "workdir", "dir": d})
        return self

    def entrypoint(self, cmd: List[str]) -> "Image":
        self._steps.append({"op": "entrypoint", "command": list(cmd)})
        return self

    def cmd(self, cmd: List[str]) -> "Image":
        self._steps.append({"op": "cmd", "command": list(cmd)})
        return self

    def add_local_file(self, src: str, dest: str) -> "Image":
        self._local_files.append(LocalCopy(src, dest))
        return self

    def add_local_dir(self, src: str, dest: str) -> "Image":
        self._local_files.append(LocalCopy(src, dest))
        return self

    def compile(self) -> CompiledImage:
        return CompiledImage(
            kind=self._kind,
            base=self._base,
            fromTemplate=self._from_template,
            packages=list(self._packages),
            steps=[dict(s) for s in self._steps],
            fragment=("\n".join(_render_dockerfile(s) for s in self._steps) + "\n")
            if self._steps
            else "",
            localFiles=list(self._local_files),
        )
