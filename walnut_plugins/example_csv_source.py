"""A worked example: expose any folder of text files as a Walnut source.

Copy this, change `FOLDER` and the parsing, and you have connected a system Walnut
has never heard of. The registry will run the conformance suite against it and tell
you which guarantees hold before it is allowed to contribute evidence.

The six methods are the whole contract. Three of them are read, three are write, and
a read-only source implements the write half by declaring one additive operation and
refusing everything else — which is exactly what this does.
"""

from __future__ import annotations

from pathlib import Path

from walnut.contract import (
    Action,
    ActionCapabilities,
    ActionReceipt,
    ActionTier,
    Evidence,
    SourcePointer,
    SourceProfile,
    content_hash,
    utcnow,
)

FOLDER = Path("walnut_plugins/example_docs")


class FolderAdapter:
    """Every .txt file in a folder becomes one cited fact."""

    name = "internal_docs"

    def __init__(self, folder: Path = FOLDER) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        # A source with nothing in it cannot demonstrate conformance, so seed one file
        # the first time. A real adapter would obviously not do this.
        sample = self.folder / "onboarding.txt"
        if not any(self.folder.glob("*.txt")):
            sample.write_text(
                "Onboarding runbook. New clinical sites are provisioned by the "
                "platform team; see the dosing engine v2 spec for current status.",
                encoding="utf-8",
            )
        self._annotations: dict[str, str] = {}

    # -- read ---------------------------------------------------------------

    def probe(self) -> SourceProfile:
        return SourceProfile(
            app=self.name,
            display_name=f"Internal docs ({self.folder})",
            scopes=(str(self.folder),),
            record_count_estimate=len(list(self.folder.glob("*.txt"))),
        )

    def _to_evidence(self, path: Path) -> Evidence:
        text = path.read_text(encoding="utf-8", errors="replace")
        return Evidence(
            id=path.stem,
            pointer=SourcePointer(
                app=self.name,
                # The conformance suite requires a followable URI. A file source has no
                # natural URL, so synthesise a stable one rather than leaving it blank.
                resource_uri=f"https://internal.docs/{path.stem}",
                locator={"id": path.stem, "path": str(path)},
                content_hash=content_hash({"name": path.name, "text": text}),
            ),
            text=text,
            occurred_at=utcnow(),
            labels=("internal_docs",),
        )

    def fetch(self, scope: str | None = None, limit: int = 100) -> list[Evidence]:
        return [self._to_evidence(p) for p in sorted(self.folder.glob("*.txt"))][:limit]

    def resolve(self, pointer: SourcePointer) -> Evidence | None:
        path = self.folder / f"{pointer.locator.get('id', '')}.txt"
        return self._to_evidence(path) if path.exists() else None

    # -- write --------------------------------------------------------------

    def capabilities(self) -> ActionCapabilities:
        return ActionCapabilities(app=self.name,
                                  operations={"annotate": ActionTier.TRIVIAL})

    def act(self, action: Action) -> ActionReceipt:
        if action.operation != "annotate":
            raise ValueError(f"{self.name} is read-only apart from annotate")
        key = str(action.target.get("id", ""))
        prior = {"note": self._annotations.get(key)} if key in self._annotations else None
        self._annotations[key] = str(action.payload.get("note", ""))
        return ActionReceipt(action_id=f"{self.name}-{len(self._annotations)}",
                             action=action, result={"id": key}, prior_state=prior)

    def undo(self, receipt: ActionReceipt) -> ActionReceipt:
        key = str(receipt.action.target.get("id", ""))
        if receipt.prior_state is not None:
            self._annotations[key] = receipt.prior_state.get("note", "")
        else:
            self._annotations.pop(key, None)
        return ActionReceipt(action_id=receipt.action_id, action=receipt.action,
                             result=receipt.result, prior_state=receipt.prior_state,
                             executed_at=receipt.executed_at, undone_at=utcnow())


def build() -> FolderAdapter:
    """The registry calls this."""
    return FolderAdapter()
