# Legacy vs New Codebase — Gap Analysis

> **Purpose:** Document every feature/capability present in the legacy `burlak_parser/` standalone CLI tool that is **completely missing or inadequately implemented** in the new FastAPI + Celery + Redis architecture (`app/`).
>
> **Date:** 2026-06-28

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [What the Legacy Code Contains](#2-what-the-legacy-code-contains)
3. [What Exists in the New Code](#3-what-exists-in-the-new-code)
4. [Critical Gaps — Module by Module](#4-critical-gaps--module-by-module)
   - [4.1 BOM Parser (bom_parser.py)](#41-bom-parser-bom_parserpy)
   - [4.2 Card Parser (card_parser.py)](#42-card-parser-card_parserpy)
   - [4.3 Heuristic Analyzer (heuristic_analyzer.py)](#43-heuristic-analyzer-heuristic_analyzerpy)
   - [4.4 Comparator / Matching Engine (comparator.py)](#44-comparator--matching-engine-comparatorpy)
   - [4.5 Report Generator (report_generator.py)](#45-report-generator-report_generatorpy)
   - [4.6 Sheet Splitter (splitter.py)](#46-sheet-splitter-splitterpy)
   - [4.7 File Classifier (file_classifier.py)](#47-file-classifier-file_classifierpy)
   - [4.8 Validator (validator.py)](#48-validator-validatorpy)
   - [4.9 XLS Converter (xls_converter.py)](#49-xls-converter-xls_converterpy)
   - [4.10 Processing Cache (cache.py)](#410-processing-cache-cachepy)
   - [4.11 Diagnostics (diagnostic.py)](#411-diagnostics-diagnosticpy)
   - [4.12 CLI Entry Point (main.py)](#412-cli-entry-point-mainpy)
5. [What Is Fully Preserved](#5-what-is-fully-preserved)
6. [Risk Assessment](#6-risk-assessment)
7. [Recommended Action Plan](#7-recommended-action-plan)

---

## 1. Executive Summary

The legacy `burlak_parser` is a mature, battle-tested CLI tool (~6,500 lines of core logic) that handles the complete BOM-to-card verification pipeline for automotive manufacturing. The new `app/` backend provides a modern web API with async job processing, but **delegates all domain intelligence to an external ML service** and currently acts as a thin orchestrator.

**Key finding:** The new system is missing **12 major functional modules** from the legacy code. The most critical gap is that `aggregate.py` (the comparison/reporting task) is a **placeholder that writes hardcoded mock data** — it does not actually compare BOM parts against parsed card data.

### Gap Severity Summary

| Severity | Count | Description |
|----------|-------|-------------|
| 🔴 CRITICAL | 4 | Core business logic entirely absent (BOM parsing, comparison, reporting, sheet splitting) |
| 🟠 HIGH | 4 | Important features missing (validation, XLS support, diagnostics, file classifier depth) |
| 🟡 MEDIUM | 3 | Quality-of-life features missing (caching, CLI, multiprocessing) |
| ✅ PRESERVED | 2 | Modules fully ported (normalizer, fuzzy_matcher) |

---

## 2. What the Legacy Code Contains

The `burlak_parser/` package contains **14 Python modules** implementing a complete pipeline:

```
burlak_parser/
├── main.py                 # CLI orchestrator (argparse, logging, progress bars)
├── bom_parser.py           # BOM xlsx parser (multi-sheet, multi-config, SWM aggregation)
├── card_parser.py          # Card parser (xlsx+xls, parallel, multi-op sheets)
├── comparator.py           # Comparison engine (4 discrepancy types, integrity check)
├── fuzzy_matcher.py        # Safe fuzzy matching (dash/space normalization only)
├── heuristic_analyzer.py   # Core intelligence (3-language headers, column detection)
├── normalizer.py           # Data normalization (quantities, part numbers, names)
├── report_generator.py     # Enterprise Excel reports (5 sheets, dashboards)
├── splitter.py             # Sheet splitter (ZIP-based, vertical split, 100% style preservation)
├── file_classifier.py      # File classification (service/operational/unknown)
├── validator.py            # 5-level output validation pipeline
├── xls_converter.py        # .xls → .xlsx via LibreOffice
├── cache.py                # File hash incremental processing cache
└── diagnostic.py           # JSON diagnostic dumps for debugging
```

### Pipeline Flow (Legacy)

```
1. Parse BOM (.xlsx) ──→ BOMData (parts, configs, quantities)
2. Discover cards (folder/ZIP) ──→ classify files
3. Parse cards (.xlsx/.xls) ──→ CardsData (parts, sources, aggregated)
4. Split multi-sheet cards ──→ individual .xlsx files (style-preserved)
5. Compare BOM vs Cards ──→ MultiConfigComparisonResult (4 discrepancy types)
6. Generate reports ──→ discrepancies.xlsx (5 sheets) + report.txt
7. Verify integrity ──→ IntegrityCheck (mathematical proof of completeness)
```

---

## 3. What Exists in the New Code

The `app/` package provides a modern web backend with these components:

```
app/
├── main.py                         # FastAPI application
├── api/v1/                         # REST endpoints (jobs, files, results, health)
├── core/                           # Config, exceptions, storage, Redis
├── db/                             # SQLite models, async/sync repositories
├── schemas/                        # Pydantic response models
├── services/
│   ├── normalizer.py               # ✅ IDENTICAL to legacy
│   ├── fuzzy_matcher.py            # ✅ IDENTICAL to legacy
│   ├── card_parser_service.py      # ⚠️ SIMPLIFIED (ML-config driven, no heuristics)
│   ├── structure_adapter.py        # NEW: ML service HTTP client
│   ├── snapshot_service.py         # NEW: Excel snapshot for ML analysis
│   ├── file_service.py             # NEW: Chunked file upload
│   ├── archive_service.py          # NEW: Simple ZIP packaging
│   ├── cache_service.py            # NEW: Redis job status cache
│   ├── notification_service.py     # NEW: Redis Pub/Sub progress
│   ├── result_service.py           # NEW: Result file path resolution
│   ├── job_creation_service.py     # NEW: Job creation
│   └── job_processing_service.py   # NEW: Job lifecycle
└── worker/tasks/
    ├── unpack.py                   # Read ZIP TOC, create card records
    ├── analyze_mapping.py          # ML structure analysis
    ├── process_card.py             # Parse + translate single card
    ├── aggregate.py                # 🔴 PLACEHOLDER: hardcoded mock data
    └── package.py                  # ZIP packaging + final status
```

### Pipeline Flow (New)

```
1. Upload files via chunked API ──→ /data/{job_id}/
2. Unpack ──→ read ZIP TOC, create card DB records
3. Analyze mapping ──→ ML service returns column coordinates
4. Process cards ──→ parse using ML coords + translate via ML
5. Aggregate ──→ 🔴 PLACEHOLDER (mock diff.xlsx)
6. Package ──→ ZIP translated cards + set final status
```

---

## 4. Critical Gaps — Module by Module

### 4.1 BOM Parser (`bom_parser.py`)

**Status:** 🔴 **COMPLETELY MISSING**

The new system extracts a "snapshot" of the BOM file for the ML service (`snapshot_service.py`) but **never actually parses the BOM data** into structured part numbers, names, and quantities. The `aggregate.py` task writes hardcoded data instead of reading real BOM contents.

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| Multi-sheet BOM parsing (iterate all sheets) | ✅ | ❌ | Entirely absent |
| SWM multi-sheet aggregation (总装/涂装/焊装 → single config) | ✅ | ❌ | Entirely absent |
| Multi-block horizontal layout detection | ✅ | ❌ | Entirely absent |
| Strikethrow filtering (skip canceled parts) | ✅ | ❌ | Entirely absent |
| Cross-sheet global name dictionary | ✅ | ❌ | Entirely absent |
| `BOMData` structure (parts, config_names, config_quantities) | ✅ | ❌ | Entirely absent |
| `BOMService` with `load_from_bytes` / `load_async` | ✅ | ❌ | Replaced by snapshot extraction only |
| Config quantity extraction (`get_config_quantities`) | ✅ | ❌ | Entirely absent |
| data_only=True fallback for formula-heavy files | ✅ | ❌ | Entirely absent |
| Deduplication of config column names | ✅ | ❌ | Entirely absent |
| Removal of qty=0 parts from final results | ✅ | ❌ | Entirely absent |

**Impact:** Without BOM parsing, the system cannot compare BOM parts against card parts — the core purpose of the entire application.

---

### 4.2 Card Parser (`card_parser.py`)

**Status:** 🟠 **SIGNIFICANTLY SIMPLIFIED**

The new `CardParserService` reads column coordinates from the ML config and iterates rows mechanically. It lacks the robustness needed for real-world manufacturing Excel files.

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| `.xls` file support (via xlrd/LibreOffice) | ✅ | ❌ | Only `.xlsx` via openpyxl |
| Multi-operation sheet parsing (multiple tables per sheet) | ✅ | ❌ | Assumes single table per sheet |
| Multiline part number merging (`-` continuation) | ✅ | ❌ | Entirely absent |
| Graphic number extraction (图示编号, Changan format) | ✅ | ❌ | Entirely absent |
| Inspection format detection (检验作业指导书) | ✅ | ❌ | Entirely absent |
| Corrupted file isolation and tracking | ✅ | ❌ | Errors logged but not isolated |
| Parallel parsing (ProcessPoolExecutor) | ✅ | ❌ | Sequential per Celery task |
| `SplitStatistics` with skip reasons | ✅ | ❌ | Entirely absent |
| `part_sources` tracking (which card → which part) | ✅ | ❌ | Not tracked |
| `original_part_numbers` preservation | ✅ | ⚠️ | Partially present |
| Section boundary detection (3+ empty rows → stop) | ✅ | ⚠️ | Simplified (3 empty rows only) |
| Skip keywords (物料清单, 变更记录, etc.) | ✅ | ❌ | Not implemented |
| `ExcelReader` abstraction (openpyxl + xlrd) | ✅ | ❌ | Only openpyxl |
| Fallback: read_only mode for corrupted files | ✅ | ❌ | Not implemented |
| `CardService` with async support | ✅ | ❌ | Sync only in Celery tasks |

**Impact:** Cards from Chinese suppliers often use `.xls` format, multi-operation sheets, and non-standard layouts. The simplified parser will fail on these files.

---

### 4.3 Heuristic Analyzer (`heuristic_analyzer.py`)

**Status:** 🔴 **COMPLETELY MISSING** (replaced by ML service)

The `HeuristicAnalyzer` class (~1,200 lines) is the core intelligence of the legacy system. It dynamically detects document structure without hardcoded column indices. The new system delegates this entirely to the ML service.

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| Header row detection (score-based, 3 languages) | ✅ | ML | No fallback if ML fails |
| Column type classification (part_no/name/qty/config) | ✅ | ML | No fallback |
| 3-language keyword dictionaries (Chinese/English/Russian) | ✅ | ❌ | Entirely absent |
| Anti-keyword filtering (supplier, factory, etc.) | ✅ | ❌ | Entirely absent |
| Content-based column detection fallback | ✅ | ❌ | Entirely absent |
| Data profiling (alpha-numeric 8-15 char detection) | ✅ | ❌ | Entirely absent |
| Config column detection (S/- markers, numeric vs VIN split) | ✅ | ML | No fallback |
| Graphic number column detection | ✅ | ❌ | Entirely absent |
| Merged cell resolution | ✅ | ❌ | Entirely absent |
| Strikethrough batch detection (`get_strike_rows`) | ✅ | ❌ | Entirely absent |
| Service sheet detection | ✅ | ⚠️ | Simplified keyword check |
| Operation name extraction from sheet headers | ✅ | ❌ | Entirely absent |
| Multi-row header scan (below and above) | ✅ | ❌ | Entirely absent |
| Part table boundary detection (`find_part_table`) | ✅ | ❌ | Entirely absent |
| False positive rejection for part_no keywords | ✅ | ❌ | Entirely absent |

**Impact:** If the ML service is unavailable, misconfigured, or returns incorrect coordinates, the new system has zero fallback capability. The legacy system could parse any Excel file without external dependencies.

---

### 4.4 Comparator / Matching Engine (`comparator.py`)

**Status:** 🔴 **COMPLETELY MISSING**

The `aggregate.py` task is a **placeholder** that creates a hardcoded mock Excel file. It does not read BOM data, does not read parsed card data, and does not perform any comparison.

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| Multi-config matrix comparison (all configs simultaneously) | ✅ | ❌ | Entirely absent |
| `ONLY_IN_BOM` discrepancy detection | ✅ | ❌ | Entirely absent |
| `ONLY_IN_CARDS` discrepancy detection | ✅ | ❌ | Entirely absent |
| `QUANTITY_MISMATCH` discrepancy detection | ✅ | ❌ | Entirely absent |
| `FUZZY_MATCH` discrepancy detection | ✅ | ❌ | Entirely absent |
| `verify_integrity()` mathematical proof | ✅ | ❌ | Entirely absent |
| Parallel config comparison (ProcessPoolExecutor) | ✅ | ❌ | Entirely absent |
| Cached fuzzy matching (pre-computed once for all configs) | ✅ | ❌ | Entirely absent |
| `MatchingEngine` service class | ✅ | ❌ | Entirely absent |
| `Discrepancy` dataclass with full metadata | ✅ | ❌ | Entirely absent |
| `ConfigComparisonResult` per-config results | ✅ | ❌ | Entirely absent |
| `MultiConfigComparisonResult` aggregate | ✅ | ❌ | Entirely absent |
| `IntegrityCheck` with config-level detail | ✅ | ❌ | Entirely absent |
| Text report formatting (`format_discrepancy_report`) | ✅ | ❌ | Entirely absent |

**Impact:** This is the **single most critical gap**. Without comparison logic, the system produces translated cards but cannot tell the user which parts are missing, extra, or have wrong quantities — which is the entire point of the BOM verification system.

---

### 4.5 Report Generator (`report_generator.py`)

**Status:** 🔴 **COMPLETELY MISSING**

The legacy system generates enterprise-grade Excel reports with 5 worksheets, rich formatting, and dashboards. The new system creates a single-sheet mock file.

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| Sheet 1: Summary dashboard (metric cards, config table) | ✅ | ❌ | Entirely absent |
| Sheet 2: Discrepancies (color-coded, auto-filter) | ✅ | ❌ | Entirely absent |
| Sheet 3: Fuzzy matches (side-by-side comparison) | ✅ | ❌ | Entirely absent |
| Sheet 4: All BOM parts (full inventory with per-config qty) | ✅ | ❌ | Entirely absent |
| Sheet 5: File errors (corrupted files with details) | ✅ | ❌ | Entirely absent |
| Color coding (red=qty mismatch, yellow=bom-only, blue=cards-only, green=fuzzy) | ✅ | ❌ | Entirely absent |
| Auto-filters on all sheets | ✅ | ❌ | Entirely absent |
| Freeze panes | ✅ | ❌ | Entirely absent |
| Custom column widths | ✅ | ❌ | Entirely absent |
| `xlsxwriter` integration | ✅ | ❌ | Uses openpyxl only |
| Text report generation (`report.txt`) | ✅ | ❌ | Entirely absent |
| ZIP archive creation for split cards | ✅ | ⚠️ | Simple ZIP only (no split cards) |

**Impact:** Users receive no actionable comparison report. The current `diff.xlsx` contains a single hardcoded example row.

---

### 4.6 Sheet Splitter (`splitter.py`)

**Status:** 🔴 **COMPLETELY MISSING**

The `splitter.py` module (~1,800 lines) is the second-largest module in the legacy codebase. It handles the critical task of splitting multi-sheet operational card files into individual single-sheet files.

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| ZIP-based sheet extraction (100% style preservation) | ✅ | ❌ | Entirely absent |
| Named range cleanup (prevents Excel corruption) | ✅ | ❌ | Entirely absent |
| Vertical split for mega-sheets (multiple operations per sheet) | ✅ | ❌ | Entirely absent |
| Table boundary detection (`find_table_boundaries`) | ✅ | ❌ | Entirely absent |
| Xinyuan (鑫源汽车) marker detection for SWM files | ✅ | ❌ | Entirely absent |
| Inspection boundary detection (检验项目 pattern) | ✅ | ❌ | Entirely absent |
| Repeating pattern boundary detection | ✅ | ❌ | Entirely absent |
| Confidence scoring for boundaries | ✅ | ❌ | Entirely absent |
| Deterministic path allocation (preallocate_split_paths) | ✅ | ❌ | Entirely absent |
| openpyxl fallback for WPS/corrupted files | ✅ | ❌ | Entirely absent |
| Copy fallback (last resort before corrupted) | ✅ | ❌ | Entirely absent |
| `SplitStatistics` with skip reasons and top-N analysis | ✅ | ❌ | Entirely absent |
| `FileSplitStats` per-file tracking | ✅ | ❌ | Entirely absent |
| Corrupted file isolation (corrupted_cards/) | ✅ | ❌ | Entirely absent |
| Manifest generation (split_manifest.json) | ✅ | ❌ | Entirely absent |
| Post-horizontal vertical split | ✅ | ❌ | Entirely absent |
| Boundary snapping (close gaps between operations) | ✅ | ❌ | Entirely absent |
| Content_Types.xml filtering | ✅ | ❌ | Entirely absent |
| Workbook view reset (activeTab, firstSheet) | ✅ | ❌ | Entirely absent |
| Parallel splitting (ProcessPoolExecutor) | ✅ | ❌ | Entirely absent |

**Impact:** Multi-sheet card files (extremely common in Chinese automotive suppliers) are processed as-is, meaning parts from different operations get mixed together, producing incorrect comparison results.

---

### 4.7 File Classifier (`file_classifier.py`)

**Status:** 🟠 **SIGNIFICANTLY SIMPLIFIED**

The new `card_parser_service.py` includes a basic `classify_file()` function, but it lacks the depth of the legacy classifier.

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| Service keyword detection (封面, 目录, etc.) | ✅ | ✅ | Preserved |
| Operational card keyword detection (作业指导书, etc.) | ✅ | ✅ | Preserved |
| Operation number regex (digits in filename) | ✅ | ✅ | Simplified |
| PREFIX_AS pattern (MODEL-A-AS-NNNNN) | ✅ | ✅ | Simplified |
| CARD_NUMBER_ANYWHERE regex (find card# in any position) | ✅ | ❌ | Entirely absent |
| LETTERS_DIGITS regex (1-3 letters + 2+ digits) | ✅ | ❌ | Entirely absent |
| OP_NUMBER_IN_FILENAME regex | ✅ | ❌ | Entirely absent |
| `FileClassification` dataclass with full metadata | ✅ | ❌ | Returns string only |
| Parent folder awareness | ✅ | ❌ | Not used |
| Heuristic card number extraction as fallback | ✅ | ❌ | Not implemented |
| Configurable rules from mapping_config | ❌ | ✅ | New feature |

**Impact:** Some card files will be classified as "unknown" and skipped, especially files with non-standard naming or encoding artifacts in the filename.

---

### 4.8 Validator (`validator.py`)

**Status:** 🟠 **COMPLETELY MISSING**

The legacy system validates every output file after splitting with a 5-level pipeline.

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| Level 1: Structural (ZIP integrity, XML well-formed, required files) | ✅ | ❌ | Entirely absent |
| Level 2: Schema (sheetData exists, row count > 0) | ✅ | ❌ | Entirely absent |
| Level 3: Content (media references, drawing integrity) | ✅ | ❌ | Entirely absent |
| Level 4: Semantic (part numbers valid, quantities numeric) | ✅ | ❌ | Entirely absent |
| Level 5: Split-quality (images preserved, row count expected) | ✅ | ❌ | Entirely absent |
| Lenient validation (openpyxl-only check) | ✅ | ❌ | Entirely absent |
| Quarantine folder isolation | ✅ | ❌ | Entirely absent |
| Batch validation | ✅ | ❌ | Entirely absent |

**Impact:** Corrupted output files may be delivered to users without detection.

---

### 4.9 XLS Converter (`xls_converter.py`)

**Status:** 🟠 **COMPLETELY MISSING**

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| LibreOffice headless .xls → .xlsx conversion | ✅ | ❌ | Entirely absent |
| Batch conversion with caching | ✅ | ❌ | Entirely absent |
| LibreOffice binary auto-discovery | ✅ | ❌ | Entirely absent |
| LIBREOFFICE_PATH environment variable | ✅ | ❌ | Entirely absent |
| Fallback: xlrd for .xls without images | ✅ | ❌ | No .xls support at all |

**Impact:** Archives containing `.xls` files (common with older suppliers) will be completely skipped.

---

### 4.10 Processing Cache (`cache.py`)

**Status:** 🟡 **COMPLETELY MISSING** (different concept in new code)

The legacy cache enables incremental processing — only changed files are re-processed. The new `cache_service.py` caches Redis job status (a completely different purpose).

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| File hash-based change detection (MD5) | ✅ | ❌ | Entirely absent |
| mtime + size quick check | ✅ | ❌ | Entirely absent |
| Stale entry cleanup | ✅ | ❌ | Entirely absent |
| Incremental processing (skip unchanged files) | ✅ | ❌ | Entirely absent |
| JSON index persistence | ✅ | ❌ | Entirely absent |

**Impact:** Every job processes all files from scratch. Not critical for one-shot web API usage, but important for iterative development/debugging.

---

### 4.11 Diagnostics (`diagnostic.py`)

**Status:** 🟡 **COMPLETELY MISSING**

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| BOM parsed dump (JSON) | ✅ | ❌ | Entirely absent |
| OC parsed dump (JSON) | ✅ | ❌ | Entirely absent |
| Schema detection dump (JSON) | ✅ | ❌ | Entirely absent |
| `DiagnosticDumper` class | ✅ | ❌ | Entirely absent |
| `--diagnostic` CLI flag | ✅ | ❌ | No CLI at all |

**Impact:** No way to inspect intermediate parsing results for debugging. Critical for diagnosing ML service issues.

---

### 4.12 CLI Entry Point (`main.py`)

**Status:** 🟡 **COMPLETELY MISSING** (replaced by REST API)

| Feature | Legacy | New | Gap |
|---------|--------|-----|-----|
| argparse CLI with all options | ✅ | ❌ | Replaced by REST API |
| Interactive config selection | ✅ | ❌ | Not applicable |
| Progress bars (tqdm) | ✅ | ⚠️ | SSE streaming instead |
| Verbose/quiet logging modes | ✅ | ❌ | Not configurable per request |
| Auto-cleanup of output directories | ✅ | ❌ | Manual cleanup only |
| File size validation with warnings | ✅ | ❌ | Not implemented |

---

## 5. What Is Fully Preserved

| Module | Status | Notes |
|--------|--------|-------|
| `normalizer.py` | ✅ IDENTICAL | All classes and functions preserved |
| `fuzzy_matcher.py` | ✅ IDENTICAL | All classes and functions preserved |

These two modules were copied verbatim into `app/services/` with only import path changes.

---

## 6. Risk Assessment

### Production Readiness: ❌ NOT READY

The new system **cannot perform its core function** (BOM verification) because:

1. **No BOM parsing** — BOM data is never extracted into structured form
2. **No comparison logic** — `aggregate.py` writes hardcoded mock data
3. **No real reports** — Users receive a meaningless `diff.xlsx`
4. **No sheet splitting** — Multi-sheet cards produce incorrect aggregated data
5. **No .xls support** — Older supplier files are silently skipped

### When ML Service Is Down: ❌ COMPLETE FAILURE

The legacy system was fully self-contained. The new system has a hard dependency on the ML service for:
- Structure analysis (column coordinates)
- Translation (Chinese → target language)

If the ML service is unavailable, **no cards can be processed at all**.

---

## 7. Recommended Action Plan

### Phase 1: Core Business Logic (CRITICAL)

1. **Port `bom_parser.py`** → `app/services/bom_parser_service.py`
   - Adapt for in-memory processing (BytesIO)
   - Keep all multi-sheet/SWM/strikethrough logic
   - Return structured `BOMData` compatible with comparison

2. **Port `comparator.py`** → `app/services/comparator_service.py`
   - Implement real comparison in `aggregate.py`
   - Support both ML-config-driven and heuristic modes
   - Include `verify_integrity()`

3. **Port `report_generator.py`** → `app/services/report_service.py`
   - Generate real 5-sheet Excel reports
   - Include text report generation

4. **Port `splitter.py`** → `app/services/splitter_service.py`
   - Adapt for Celery worker context (sync, per-file)
   - Preserve ZIP-based splitting and vertical split

### Phase 2: Robustness (HIGH)

5. **Port `heuristic_analyzer.py`** as ML fallback
6. **Port `xls_converter.py`** for .xls support
7. **Port `validator.py`** for output validation
8. **Enhance `file_classifier.py`** with full regex set

### Phase 3: Quality of Life (MEDIUM)

9. **Port `cache.py`** for incremental processing
10. **Port `diagnostic.py`** for debugging
11. Add optional CLI interface for direct usage

---

*This document should be updated as gaps are addressed.*
