"""reading-companion backend — FastAPI service productionizing the validated LIT cores.

Package layout (ADR 0007): memory/ (LIT-5 store + DAL), catalog/ (LIT-18 global catalog),
ingest/ (LIT-4 segmentation + LIT-6 extraction), llm/ (LIT-20), reader/ (LIT-12 frontier),
eval/spoiler_gate/ (LIT-8 gate, shared with runtime), api/ (routes).
"""

__version__ = "0.1.0"
