"""Apply the small UI extension to the pinned upstream source; fail on drift."""
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1])
overlay = Path(__file__).parent
business = source / 'src/modules/business'
for name in ('linkedKnowledge.ts', 'KnowledgePanel.tsx'):
    shutil.copyfile(overlay / name, business / name)


def replace(path, before, after):
    text = path.read_text()
    if text.count(before) != 1:
        raise RuntimeError(f'Pinned Cognee UI contract changed: {path.name}: {before!r}')
    path.write_text(text.replace(before, after))


panel = business / 'panels/NodePanel.tsx'
replace(panel, 'interface NodePanelProps {', 'import KnowledgePanel from "../KnowledgePanel";\nimport type { KnowledgeRecord } from "../linkedKnowledge";\n\ninterface NodePanelProps {\n  knowledge: KnowledgeRecord[];')
replace(panel, 'entity, docCount, connectionCount, events, onClose,', 'entity, knowledge, docCount, connectionCount, events, onClose,')
replace(panel, 'w-72 rounded-xl', 'w-[420px] max-w-[calc(100%-2rem)] max-h-[calc(100%-6rem)] overflow-y-auto rounded-xl')
replace(panel, '      {/* The shortest-path trace', '      <KnowledgePanel records={knowledge} />\n      {/* The shortest-path trace')

view = source / 'src/app/(app)/business/BusinessView.tsx'
replace(view, 'const WHAT_IF_SPOTLIGHT_MS', 'import { linkedKnowledge } from "@/modules/business/linkedKnowledge";\n\nconst WHAT_IF_SPOTLIGHT_MS')
replace(view, '          entity={selection.selectedEntity}', '          entity={selection.selectedEntity}\n          knowledge={linkedKnowledge(selection.selectedEntity.id, scene.layerData[CONTENT_LAYER_ID])}')
