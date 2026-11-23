Standalone portable release for Windows x64 featuring NVIDIA RTX GPU acceleration and universal CPU fallback.

### What's New in v1.5.1
- **Full-Group Deletion Safeguard & Balanced Confirmation Dialogs (`results_table.py`, `main_window.py`, `theme.py`, `main.py`):**
  - Added detection for duplicate groups where all copies are checked (`get_fully_selected_group_count`), displaying a bold `ATTENTION:` warning whenever an action would leave zero copies behind (`Trash`, `Move`, and `Permanent Delete`).
  - Redesigned confirmation dialogs for a cleaner, consistent layout across all actions (`NoIcon` body with balanced 16 px horizontal margins, crisp vector warning triangle in the title bar, RichText bold emphasis, `?` titles, and always-underlined `&Yes` / `&No` mnemonics on Windows via `AlwaysUnderlineShortcutStyle`).
- **Windows Permanent Delete Resilience & Streamlined Status Completion (`worker.py`, `main_window.py`):**
  - Hardened `_permanent_delete_file` with extended-length Windows paths (`\\?\`), trailing dot/space handling, Unicode normalization tolerance, and automatic clearing of Read-Only (`FILE_ATTRIBUTE_READONLY`) flags before deletion.
  - Replaced post-action `OK` popups on successful Trash, Move, and Delete operations with immediate status bar summaries (error warnings still surface explicitly).
- **Portable Root `logs/` Directory (`crash_logger.py`, `main.py`, `build.bat`, `.gitignore`):**
  - Relocated application crash logs from `data/logs/` to `<root>/logs/` cleanly separating logs from `data/` and `lib/` in standalone portable builds.

### What's New in v1.5.0
- **Per-Folder File Count Column & Auto-Grouped Reference Folders (`folder_panel.py`, `scanner.py`, `worker.py`, `main_window.py`):**
  - Added a `Files` column to the **Folders to Scan** table (`Ref` | `Folder Path` | `Files`) showing exact per-folder file counts discovered during Stage 1 with zero extra disk I/O.
  - Automatically groups `Ref`-checked folders at the top of **Folders to Scan** while preserving relative order, `Files` counts, and a clean transparent cell background.
  - Automatically resets counts when starting a new scan or switching scan modes.
- **Human-Readable Reclaimable Space (`main_window.py`):**
  - Updated the scan completion status summary (`Estimated space to free`) to use `format_size()`, automatically scaling from `MB` to `GB` / `TB` above 1 GB (e.g., `1.20 GB`).
- **Non-Blocking Post-Scan Cache Maintenance (`worker.py`, `cache.py`, `main_window.py`):**
  - Moved post-scan orphan pruning, conditional `VACUUM`, and WAL `checkpoint()` into an independent `CacheMaintenanceWorker` outside `is_busy`, eliminating 3–5 second UI stalls after scan completion while supporting safe interruption before new scans, cache clears, or exit.
  - Optimized Windows path normalization in `prune_scanned_roots` for faster scans on large SQLite databases.

### What's New in v1.4.2
- **Automatic Cache Orphan Pruning & Conditional VACUUM (`cache.py`, `worker.py`):**
  - Automatically removes deleted file paths (`remove_paths`) and relocates moved file paths in-place (`relocate_paths`) across all 3 cache tables (`cache_exact`, `cache_images`, `cache_videos`) right inside `FileActionWorker` after successful Trash, Delete, or Move operations.
  - Added safe Tier L3 dead-source cleanup with mounted-volume protection (`_is_volume_mounted`) so disconnected external drives never lose valid cached embeddings.
  - Added non-blocking post-scan root pruning (`prune_scanned_roots`) for the active scan mode, respecting excluded folders and `LIKE` wildcards, with automatic `VACUUM` whenever freed SQLite pages reach `>= 25%` to reclaim disk space physically.

### What's New in v1.4.1
- **UI Branding & Layout Polish (`splash.py`, `main_window.py`, `help_dialog.py`, `README.md`):**
  - Updated branding across the splash screen, window title, About dialog, Help window, and documentation from `GPU-Accelerated` to `GPU-Powered`.
  - Centered the `⚡ DupeClean` logo in the left sidebar, removed the redundant sidebar subtitle, and moved the version indicator cleanly to the window title (`DupeClean - GPU-Powered Duplicate File Finder - v1.4.1`).
  - Corrected the initial status prompt from `"Add folders below."` to `"Add folders above."` to match the folder panel position.

### What's New in v1.4.0
- **Collapsible Left Sidebar (`main_window.py`):**
  - Added a clean, floating `‹` / `›` toggle arrow centered vertically on the sidebar divider line (zero extra layout columns, preserving top action bar alignment).
  - Collapsing the sidebar hides the 220 px panel completely and lowers the minimum window width to 680 px for comfortable side-by-side split-screen comparison with Windows Explorer.
  - Persisted the collapsed/expanded preference (`sidebar_collapsed`) in `DupeClean.db` (`app_settings`).

### What's New in v1.3.7
- **Eliminated 5-Second Cache Flush Stalls & UI Progress Stuttering (`hasher_exact.py`, `hasher_image.py`, `hasher_video.py`):**
  - Fixed `_AsyncExactCacheWriter`, `_AsyncImageCacheWriter`, and `_AsyncVideoCacheWriter` so `close()` drains queued items in full batches instead of issuing one SQLite connection/commit per item (which previously hit the 5-second `join()` timeout on large queues).
  - Enforced strict 0.1s (10 Hz) UI progress throttling during `Calculating partial hashes` and `Calculating full hashes` to eliminate Qt repaint flooding.
  - Tuned exact I/O worker concurrency (`16` header / `8` deep) and replaced `Path.parent` allocations in directory-locality sorting with direct string sorting.

### What's New in v1.3.6
- **Faster Cache Lookups & Moved/Renamed Folder Resilience (`cache.py`):**
  - Switched `_prefilter_known_sizes` to 100% covering-index scans (`idx_*_size`), avoiding full-table BLOB page reads on SQLite.
  - Updated Tier L2 (`cache_exact`, `cache_images`, `cache_videos`) to perform in-place `UPDATE OR REPLACE` path renames without rewriting embedding BLOBs, and added parent-directory existence caching in `_cleanup_dead_paths` (reducing renamed folder checks to a single syscall per folder).
  - Parallelized Tier L3 `file_id` collection and removed unused heavy imports (`torch`, `numpy`) from `cache.py`.
- **Responsive Video & Image Stage Transitions (`_video_worker.py`, `hasher_video.py`, `hasher_image.py`):**
  - Isolated `_worker_decode_path` into a PyTorch-free worker module (`_video_worker.py`) so spawned Windows decoding processes no longer stall for 10+ seconds loading CUDA/PyTorch DLLs.
  - Added dynamic worker/chunk sizing with an in-process fast path for small batches and immediate Stage 3 progress emission before decoding begins.

### What's New in v1.3.5
- **Context Menu "Open Containing Folder" Fix (`results_table.py`):**
  - Fixed Windows Explorer `/select,` argument quoting so paths containing spaces or special characters properly open and highlight the target file instead of falling back to the default Documents folder.
  - Switched to non-blocking `subprocess.Popen` and added a fallback to open the parent directory if the file was moved or deleted.

### What's New in v1.3.4
- **Processed vs. Total File & Byte Progress Telemetry (`scanner.py`, `hasher_exact.py`, `hasher_image.py`, `hasher_video.py`, `worker.py`, `models.py`):**
  - Replaced elapsed timer and `it/s` / `files/s` rate counters with clear processed-vs-total file count and byte volume progress across all scan stages (e.g., `Calculating full hashes of 1,900/6,720 files (76.72 GB / 1.61 TB)...`).
  - Removed `lbl_timer`, `ui_timer`, and `_update_telemetry_tick` from `MainWindow` and removed the `Throughput (it/s)` section from `HelpDialog`.

### What's New in v1.3.3
- **Instant Background Cache Reset & Inline Sidebar Confirmation (`cache.py`, `worker.py`, `main_window.py`):**
  - Replaced slow row-by-row `DELETE FROM` + `VACUUM` on full cache clears with `reset_database_preserving_settings` (instant file unlink/recreation preserving `app_settings`) and `DROP TABLE` + recreate for single-mode clears.
  - Added `CacheClearWorker` (`QThread`) so clearing massive caches never blocks the UI thread or triggers Windows "Not Responding".
  - Replaced modal `QMessageBox` dialogs with a sleek inline **`Clear Cache / Are You Sure? [Yes] [No]`** confirmation card in the sidebar (`No` focused and highlighted as the default button), eliminating modal focus/shadow flicker.
- **Documentation & Help Clarity (`help_dialog.py`, `README.md`):**
  - Documented the purpose of **Clear Cache** (freeing disk space or forcing full recomputation, noting that subsequent scans will be slower until rebuilt).
  - Clarified Stage 3 `it/s` throughput behavior across different file sizes and types (HDD, SSD, or NVMe).

### What's New in v1.3.2
- **Context-Aware Button & Control States (`main_window.py`, `folder_panel.py`, `test_ui_button_states.py`):**
  - Disabled `Scan` when no scan folders are queued, disabled `Select` and `Save` when the results table is empty, and disabled `- Remove Selected`, `Clear All`, and `- Remove` (Excluded) whenever those actions have nothing to act on.
  - Synchronized button states dynamically across folder additions/removals, scan completion, and post-delete/trash/move cleanup.
- **Help Documentation Polish (`help_dialog.py`):**
  - Simplified column-sorting instructions to describe double-click ascending/descending (`▲` / `▼`) and clear behavior cleanly.

### What's New in v1.3.1
- **Unified Centered Hardware Badge (`main_window.py`):**
  - Unified GPU (`#4ade80`) and CPU fallback (`#a1a1aa`) hardware badges onto a single centered layout with a crisp vector-rendered `12×12` antialiased status dot (`_create_status_dot_pixmap`) and `padding: 0px` QSS isolation.
- **Clean Build Spec & Source Dependency Completeness (`dupeclean.spec`, `requirements.txt`, `README.md`):**
  - Removed unused `torchvision` references from `dupeclean.spec` `hiddenimports` and installation docs.
  - Added `av>=17.0.0` (PyAV) to `requirements.txt` and added CPU-only PyTorch installation instructions to `README.md` / `requirements.txt` so running directly from source works out of the box on both RTX and CPU-only systems.

### What's New in v1.3.0
- **Universal CPU Fallback for Similar Images & Similar Videos (`hardware.py`, `hasher_image.py`, `hasher_video.py`, `matcher.py`, `main.py`, `main_window.py`):**
  - Removed the startup hardware block so all 3 scan modes (`Duplicate Files`, `Similar Images`, `Similar Videos`) run cleanly on systems without a compatible NVIDIA RTX GPU using AVX-accelerated FP32 CPU tensors while preserving full FP16 SQLite cache portability (`DupeClean.db`).
  - Added a minimalist gray **`● NO GPU`** badge (`#a1a1aa`) in CPU fallback mode with tooltip guidance recommending an NVIDIA RTX GPU for superior performance on large libraries.
  - Updated in-app `HelpDialog`, About dialog, and `README.md` to document continuous 2D DCT embeddings (`768-D` / `3840-D`) and dense all-against-all (`N × N`) matching across GPU and CPU.
- **Global Interactive Zoom & Preview Pan (`results_table.py`, `preview_panel.py`, `help_dialog.py`, `main_window.py`):**
  - Added `Ctrl + Scroll` and `Ctrl` `+` / `-` / `0` zoom scaling across the Results Grid (`ResultsTreeWidget`), Media Preview (`AspectRatioPixmapLabel` with click-drag panning when zoomed), and Help Reader (`ZoomableBrowser` with viewport-independent CSS scaling and `QShortcut`s).
- **Stage 2 Cache Progress & Status Telemetry Polish (`cache.py`, `hasher_exact.py`, `main_window.py`, `splash.py`, `dupeclean.spec`):**
  - Eliminated Stage 2 progress bar stalls (`100%` freeze and decaying fake `it/s`) by prefiltering unique sizes before `get_volume_capabilities`, reporting monotonic `0% -> 50% -> 100%` progress via `_CacheProgressTracker`, and dropping throughput display when stalled.
  - Updated startup splash text to `"Warming up..."` and shortened the scan-completed status message to end cleanly at `Estimated space to free: X MB.`.
  - Removed unused `bin/ffmpeg.exe` and `bin/ffprobe.exe` executables (~434 MiB uncompressed) from `dupeclean.spec`, as all video decoding runs directly through PyAV (`av.libs`).

### What's New in v1.2.1
- **In-App Help Dialog Visual Polish (`help_dialog.py`):**
  - Rounded the corners of the Help `QTabWidget::pane` container (`border-radius: 6px`) and switched the embedded `ZoomableBrowser` body/viewport background to transparent so rounded pane borders render cleanly without square inner corners.

### What's New in v1.2.0
- **Virtualized Results Grid (`QTableView` + `QAbstractTableModel`) (`results_table.py`, `theme.py`):**
  - Migrated the results grid from `QTreeWidget` to a lightweight, fully virtualized `QTableView` backed by `ResultsModel`, `_ResultsCellDelegate`, and `_GroupEntry` with $O(1)$ lazy visible-viewport rendering across hundreds of thousands of rows.
  - Preserved all 3-state double-click column sorting modes, collapse/expand controls, custom checkbox state tracking, and keyboard/selection preview signals with instant mounting and near-zero memory overhead on massive libraries.
- **Exact Duplicate Fast-Path & Deterministic Cache Progress (`hasher_exact.py`, `cache.py`, `hasher_image.py`, `hasher_video.py`):**
  - Raised `FAST_PATH_SIZE` from `8 KB` to **`256 KB`**, allowing small and medium files (such as photos and documents) to compute their full `xxHash64` digest in a single pass during Stage 2 without reopening in Stage 3, while larger files (`> 256 KB`) retain the 8 KB Head+Tail sieve.
  - Fixed `Checking cache` callbacks across Exact, Image, and Video caches to emit deterministic `(current, total)` counts (`0% -> 100%`) instead of `(0, 0)` indeterminate marquee mode.
  - Renamed Exact Mode stage messages for clarity (`Calculating partial hashes` in Stage 2 and `Calculating full hashes` in Stage 3) and aligned in-app Help (`help_dialog.py`) and `README.md` with accurate CPU thread/process scaling and streaming buffer architecture.

### What's New in v1.1.15
- **Visual Signature Precision & Image Engine v2 (`hasher_image.py`, `db_schema.py`):**
  - Applied opaque alpha compositing (`_to_opaque_rgb`), EXIF orientation normalization (`exif_transpose`), JPEG `draft()` fast-decode prescaling, Hamming resampling, `ALPHA = 1.2` whitening calibration, and FP32 GPU DCT/L2 normalization quantized cleanly to FP16 embeddings.
  - Bumped database schema to `v2` (`_migrate_1_to_2`), invalidating only `cache_images` once on startup while preserving `cache_exact`, `cache_videos`, and `app_settings`.
- **High-Throughput Image I/O & GPU Tensor Core Clustering (`cache.py`, `matcher.py`):**
  - Eliminated Stage 4 CPU↔GPU synchronization bottlenecks with batched Tensor Core GEMM leader scanning, fast paths for pairs (`k == 2`), active pool compaction, and batched `torch.bmm` precomputation for multi-member cliques (`3 <= k <= 96`).
  - Optimized single-read image streaming thresholds, tombstones for unreadable/corrupt images, and asynchronous prefix-hash caching.
- **UI Responsiveness, Flicker Fix & Status Polish (`main_window.py`, `preview_panel.py`, `results_table.py`, `splash.py`, `theme.py`):**
  - Eliminated preview thumbnail resize flicker when switching files (`QStackedLayout` + `QSizePolicy.Ignored`) and kept the previous frame visible during rapid switches.
  - Fixed missing native application icon on startup splash screen/taskbar.
  - Simplified the similarity slider container border styling, unified stage progress labels (`Checking cache...`, `Loading results...`), and removed redundant similarity labels from group header rows (preserving the `Similarity` column and sorting).

### What's New in v1.1.14
- **Startup Database Schema Validation & Migration (`db_schema.py`):**
  - Added versioned SQLite schema tracking (`PRAGMA user_version`) and structural validation (tables, primary keys, columns, and partial indexes) on startup.
  - Legacy databases are adapted silently during initialization, missing indexes are auto-repaired, and unrecoverable/corrupted databases reset cleanly while preserving `app_settings` (such as saved folders).
  - Separated database initialization errors and future-version (`v > current`) detection from GPU hardware errors in `main.py` with explicit user prompts.

### What's New in v1.1.13
- **Stage 2 Status Feedback Polish:**
  - Refined the Tier 2 cache lookup status label to `"Checking cache index (N)..."` for clear, accurate feedback across both newly added and moved files.

### What's New in v1.1.12
- **Cache Query & Stage Transition Optimization:**
  - Fixed SQLite partial-index predicates (`file_id != 0` and `prefix_hash IS NOT NULL`) across L2/L3 cache lookups in Exact, Image, and Video modes, replacing full table scans with direct index searches (~10x faster L2 SQL queries).
  - Chunked parallel `os.stat` metadata collection during L2 cache verification (`pool.map` in batches of 2,000) and eliminated redundant volume capability checks (~7x faster L2 `os.stat` on 193k files).
  - Added immediate Stage 2 progress reporting during Exact cache validation (`get_exact_cache`) to eliminate UI status lag between directory crawling and hashing.

### What's New in v1.1.11
- **Tri-Tier Cache Resilience for Moved and Renamed Folders:**
  - Implemented lazy NTFS File ID (`volume_id + file_id`) tracking across Exact, Image, and Video modes so moving or renaming folders on the same NTFS/ReFS volume reuses existing cache entries with zero file re-reads or GPU reprocessing.
  - Added automatic batch L1 backfill for legacy databases, hardlink-safe orphan cleanup (`os.path.exists` guard), and a parallelized L3 content-sieve (`size + 8KB xxHash64 prefix`) fallback for exFAT/FAT32 drives and cross-volume copies.
  - Offloaded `prefix_hash` and `os.stat` metadata collection inside image threads and video `ProcessPoolExecutor` workers to avoid blocking the GPU consumer pipeline.

### What's New in v1.1.10
- **Polished Portable Distribution Layout (`lib/`):**
  - Customized PyInstaller's internal contents directory structure from the default `_internal` to standard `lib/` (`contents_directory='lib'`).
  - Standalone portable archive now presents a clean, elegant two-item top-level structure: `DupeClean.exe` alongside the `lib/` dependencies directory.

### What's New in v1.1.9
- **Technical Documentation & In-App Help Synchronization:**
  - Synchronized in-app Help Dialog with latest physical NVMe benchmark performance, documenting sustained sequential read speeds up to **3.4+ GB/s** via unbuffered zero-copy DMA streaming (`buffering=0` with `readinto`).
  - Added comprehensive hardware telemetry FAQs explaining dynamic CPU worker thread scaling (preserving operating system headroom) and adaptive GPU VRAM block management (256 MB caps on 8 GB cards to prevent WDDM paging vs. 2.0 GB blocks on 24/32 GB RTX cards).
  - Documented interactive rich-text viewer zoom shortcuts (`Ctrl + Scroll`, `Ctrl + / -`, and `Ctrl 0` to reset) in the Help shortcuts reference.
- **Repository Documentation Clean-Up:**
  - Eliminated redundant hardware telemetry FAQ blocks in project documentation.

### What's New in v1.1.8
- **Refined Action Bar & Unified Card Aesthetics:**
  - Restyled the top action bar into a single framed container matching the exact visual styling of the sidebar modes (`#202024` surface, `#2e2e33` subtle border, 6px radius).
  - Polished action buttons with transparent backgrounds, 500-weight typography, and rounded hover highlights (`#27272a`).
  - Standardized the Select dropdown button to use Qt's native menu indicator arrow, eliminating redundant double-arrow artifacts.
- **Intuitive Stop Control Icon:**
  - Replaced generic square stop icon with a clear red stop sign (`🛑 Stop`) for instant visual distinction from other toolbar actions.
- **Optimized Workspace & Sidebar Dimensions:**
  - Slimmed the left navigation sidebar width from 260px down to 220px, reclaiming 40px of valuable horizontal space for duplicate file listings, metadata columns, and similarity scores.
  - Preserved circular help button (`?`) positioning and functionality in top-right corner.

### What's New in v1.1.7
- **Massive I/O Performance Surge on Modern Solid-State Storage (NVMe / SSD):**
  - Eliminated ThreadPoolExecutor synchronization barriers and waiter registration GIL overhead, replacing them with a zero-friction producer-consumer queue pipeline (`queue.Queue`) sustaining over 20,000 files/sec.
  - Implemented zero-copy streaming DMA reads (`readinto`) using static pre-allocated 512 KB bytearray buffers with unbuffered I/O (`buffering=0`), unlocking raw sequential solid-state bandwidth from 278 MB/s up to **3.4+ GB/s** on high-speed NVMe drives (+1,123% throughput gain).
  - Scaled worker pool to 32 concurrent threads to fully saturate high-core CPUs and deep NVMe command queues.
- **Asynchronous Decoupled SQLite WAL Persistence Across All Modes:**
  - Decoupled SQLite database commits from I/O workers and GPU tensor batches across all three scanning modes (`_AsyncExactCacheWriter`, `_AsyncImageCacheWriter`, `_AsyncVideoCacheWriter`).
  - Batch records and tombstones are queued in memory and committed on dedicated background threads, completely eliminating worker starvation and GPU idling pauses.
- **Intuitive Telemetry & Natural Pipeline Progression:**
  - Standardized exact scan stage progression:
    - Stage 1/3: `Scanning folders (X/Y)...`
    - Stage 2/3: `Calculating file signatures (X/Y)...`
    - Stage 3/3: `Calculating hashes (X/Y)...`
  - Fixed stage numbering transition bug so Stage 3 deterministic progression is strictly guaranteed.
  - Resolved `display_title` referenced before assignment error when mounting scan results into the UI table.
  - Cleaned up global unhandled error dialogs to follow concise error reporting standards.
- **Spatial Directory Locality (NTFS Optimization):**
  - Applied parent directory sorting across all media pipelines to minimize random MFT lookups and directory block seeking.

### What's New in v1.1.6
- **Proportional Window Sizing & Window Controls for Help Dialog:**
  - Dynamic sizing scaled to ~82% width and ~86% height of the screen/parent window (minimum 780x520), ensuring spacious reading without cramped text areas.
  - Enabled native Windows maximize, minimize, and restore window buttons (`WindowMaximizeButtonHint`, `WindowMinimizeButtonHint`).
- **Interactive Multi-Level Dynamic Zoom:**
  - Added full zooming capabilities via `Ctrl + Mouse Wheel` and keyboard shortcuts (`Ctrl +`, `Ctrl -`, `Ctrl 0` to reset).
  - Added helpful status indicator in top-bar informing users of zoom controls.
- **Enhanced Typography & Readability:**
  - Upgraded font sizes (`13.5px` body, `14.5px` - `17px` headings), improved line height (`1.6`), generous paragraph and section margins, and clean, high-contrast syntax highlighting for code snippets.
  - Eliminated nested card backgrounds for clean, unified dark surfaces conforming strictly to the KISS design principle.
- **Pixel-Perfect Authentic Connected Tabs & Grid Alignment:**
  - Restored genuine, connected notebook tabs attached directly to the content frame with flat bases and top rounded corners.
  - Eliminated Qt `QTabWidget::pane` corner clipping artifacts with solid, continuous 90-degree border rendering.
  - Guaranteed 100% horizontal grid alignment across all visual elements (logo, tabs, content pane, and footer action buttons align precisely on the 18px / 862px margins).
- **License & Documentation Refinements:**
  - Formalized GNU General Public License v3.0 (GPLv3) licensing across headers and repository documentation.

### What's New in v1.1.5
- **Comprehensive Help & Technical Documentation Dialog (`HelpDialog`):**
  - Integrated full technical architecture and user guide directly into the application, accessible via the top-bar `?` button.
  - Divided into 5 structured, rich-text tabs:
    1. **Product Overview:** Value proposition of GPU acceleration vs legacy CPU deduplicators, safety guarantees with `Send2Trash`, and protected reference folders (`Ref`).
    2. **Technical Pipeline & Stages:** In-depth breakdown of Stage 1 (size grouping), Stage 2 (4 KB head+tail composite sieve), Stage 3 (512 KB stream hashing & disk saturation), and GPU clustering, plus hardware allocation FAQ.
    3. **Scan Modes & Algorithms:** Technical mechanics of Exact Duplicates (xxHash64), Similar Images (2D DCT on PyTorch CUDA FP16), and Similar Videos (NVDEC hardware acceleration, keyframe sampling, and mutual similarity).
    4. **Shortcuts & Tips:** Guide to 3-state interactive column sorting, spacebar preview, smart selection tools, and drag-and-drop.
    5. **Contact Information:** Official developer and business information for **Dominio Digital** (`contato@dominiodigital.dev.br`) and GitHub links.
- **Direct 1-Click Help Access:**
  - Modern circular `?` button on the top toolbar styled with custom dark theme palette (`#202024` bg, `#38bdf8` hover/active).
  - Opens the Help Dialog directly in a single click, eliminating extra navigation menus.
- **Refined Results Table Header Geometry:**
  - Fixed Qt header styling where table header sections had unrounded square corners that bled outside the parent table's `border-radius: 6px`.
  - Configured last section stretch (`setStretchLastSection(True)`) and calibrated column widths to guarantee full horizontal divider line continuity without gaps across all display resolutions.

### What's New in v1.1.4
- **Pixel-Perfect Smooth Progress Bar (`SmoothProgressBar`):**
  - Replaced Qt's native `QProgressBar` with a custom anti-aliased `SmoothProgressBar` adhering strictly to rounded pill geometry (`border-radius: 4px` groove, `3px` inner chunk).
  - Fixed Qt Stylesheet rendering defect where indeterminate chunks lost border radius and bled outside the groove as unrounded 90-degree rectangles.
  - Indeterminate scanning mode features an elegant pill chunk gliding continuously from left to right with seamless marquee wrapping at 50 FPS.
  - Determinate mode utilizes strict inner clipping (`QPainterPath.setClipPath`) to guarantee zero overflow or visual bleeding at any percentage.
- **Accurate & Transparent Telemetry (Stage 3):**
  - Updated Stage 3 progress description to `Reading & calculating file hashes ({current}/{total})...`.
  - Transparently communicates high-throughput parallel disk reading and CPU hashing in progress before duplicate grouping and results rendering.
- **Automated Test Coverage:**
  - Added dedicated unit test suite for `SmoothProgressBar` in `tests/test_progress_bar.py`, bringing the automated test suite to 15 passing tests.

### What's New in v1.1.3
- **Interactive High-Performance Column Sorting (3-State Cycle):**
  - Instantaneous column header double-click sorting across all results columns with a clean 3-state cycle:
    1. First double-click: **Ascending** order with native Qt indicator.
    2. Second double-click: **Descending** order with native Qt indicator.
    3. Third double-click: **Normal / Unsorted** (restores exact original scan order and clears indicator).
  - Clean headers utilizing Qt's native sort indicator without artificial appended text arrows (`▲`/`▼`).
  - Strict preservation of all checked files and selection counts across sorting states.
- **In-Place Qt Tree Sorting Architecture (`sortItems`):**
  - Replaced destructive `takeChildren()` detach/re-attach routines with Qt's native in-place `tree.sortItems(col, order)`.
  - Swaps C++ item pointers directly without invalidating persistent model indices, eliminating recursive item detachment overhead.
  - Pre-cached comparison keys and tuples (`k_size_tuple`, `k_name_tuple`, `k_mtime_tuple`, `k_sim_tuple`, `k_path_tuple`) eliminate millions of dynamic allocations during comparisons, dropping sorting time on massive datasets (~222k groups / 445k files) from ~7.4s down to ~1.9s (and instantaneous on standard datasets).
- **Zero-Latency Visual Feedback & Status Bar Telemetry:**
  - Double-clicking any column header instantly triggers the Windows wait cursor (`WaitCursor`) with 0ms visual latency via immediate event processing (`processEvents`).
  - Bottom status bar displays real-time informative feedback (e.g. `Sorting results by Size (Ascending)...` or `Restoring original scan order...`), cleanly restoring previous status and selection metrics upon completion.
- **Visual Design & Splitter Styling:**
  - Removed splitter handle background bleed, ensuring clean, rounded borders (`border-radius: 6px`) across all results grids and panels.

### What's New in v1.1.2
- **High-Throughput Exact Duplicate Engine (Multi-Stage Sieve Architecture):**
  - Standardized multi-stage hashing pipeline with 4 KB head + 4 KB tail composite pre-hash and 8 KB single-pass fast-path.
  - Spatial directory locality sorting to maximize OS disk cache hits and sequential I/O on SSD RAID arrays, HDDs, and network storage.
  - Asynchronous continuous sliding-window worker pipeline eliminating chunk barrier stalls.
  - 512 KB stream read buffering delivering sustained sequential read speeds up to 1,400+ MB/s.
  - Strict small-file cache isolation preventing small-file leakage into the full-hash stage.
- **Clean & Minimalist Visual Telemetry (KISS):**
  - Removed volatile ETA calculations, decorative badges, and timer emojis for a clean, cohesive look matching left status indicators.
  - Display format standardized to `{rate} it/s • {elapsed}` during scans and `{rate} files/s • {elapsed}` during file operations.
  - Continuous telemetry throughout the results mounting stage (`Loading results...`), with final `Total Time` and dual 100% progress bars synchronized to the exact completion moment.
- **Visual Grid Cleanliness & Protected Reference Rows:**
  - Removed the redundant "Origin" column from the duplicate results tree, streamlining results into 6 focused columns.
  - Protected Reference files are now visually distinguished across the entire row with a muted background style and protective tooltip, removing lock icons and keeping the selection column clean.
  - Reference filtering automatically discards internal reference-to-reference duplicate clusters, ensuring reference folders act exclusively as master anchor targets against target folders.
- **Complete Visual Consistency (Images vs. Photos):**
  - Harmonized all stage progression messages, tooltips, and persistent cache dialogs to "Images", maintaining 100% coherence with the "Similar Images" sidebar mode.
- **Two-Tier Progress Bar Synchronization & Responsive File Actions:**
  - Standardized completion logic to guarantee both `Current Stage` and `All Stages` reach 100% on scan completion without premature freezes.
  - Background asynchronous worker for file actions (`Move`, `Recycle Bin`, `Permanent Delete`) with real-time file-rate telemetry and non-blocking UI.

### What's New in v1.1.1
- **Strict Mutual Similarity Grouping:**
  - Enforces that every file within a duplicate group must directly match all other files in that group (pairwise mutual similarity ≥ threshold).
  - Eliminates false-positive chaining, where dissimilar files (such as an empty room and a clip with people) could previously be merged into the same group through intermediate similar frames.
- **Robust Video Feature Extraction & PyAV Demuxing:**
  - Multi-point keyframe sampling across the video timeline with frequency hashing (2D DCT) and adaptive frequency filtering.
  - Fixed dynamic stream descriptor demux errors on surveillance `.dav` video clips.
  - Enforced strict RGB array normalization, resolving shape mismatch crashes (`ValueError: all input arrays must have the same shape`) when infrared night-vision clips are processed in batches alongside daytime clips.
- **Corrupted File Auto-Skipping (Zero-Churn Cache):**
  - Unreadable, 0-frame, or corrupted video files are automatically cached as permanently unreadable (`duration = -1.0`).
  - Subsequent scans instantly skip unreadable files in ~2ms from RAM/cache, eliminating redundant disk I/O, CPU decompression cycles, and scan churn.
- **Instantaneous Telemetry & Sliding-Window Throughput:**
  - Replaced cumulative average rate with a 3-second sliding window for instantaneous throughput (`it/s`) during long scans.
- **Crash Diagnostics & Clean Error Dialogs:**
  - Added timestamped error diagnostics to `data/logs/` with sensitive path and username sanitization.
  - Refined error presentation ("An error report has been saved in data/logs.").
- **UI & Grid Stability:**
  - Interactive column width resizing with native cursor feedback across all result table headers.
  - Safe folder state clearing and preview reset when comparison sources are cleared.

### What's New in v1.1.0
- **Responsive Media Preview Panel:**
  - Integrated horizontal splitter on results table with a dedicated side preview panel that stays hidden until a media file is selected.
  - Single-click or keyboard navigation smoothly activates the preview panel for both photos and videos.
  - Preserves 100% of double-click functionality to launch files in default Windows applications (`os.startfile`), as well as right-click context menu options (`Open File`, `Open Containing Folder`).
  - **Photos:** High-resolution asynchronous loading with automatic EXIF orientation normalization (`ImageOps.exif_transpose`), preventing smartphone portrait photos from displaying sideways.
  - **Videos:** Instant keyframe extraction via FFmpeg pipe rendering full-color poster frames with resolution and duration telemetry.
  - **Zero-Latency In-Memory LRU Cache:** 60-item RAM cache providing 0ms instant switching between original and duplicate pairs without disk re-reads.
  - **KISS Metadata Card:** Compact info box displaying file name, resolution/dimensions, format, and disk size, free of redundant action buttons.
- **Unified Progress Bar Aesthetics:**
  - Standardized progress fill colors for both `Current Stage` and `All Stages` to consistent sky-cyan (`#38bdf8`) with rounded geometries.
  - Harmonized percentage indicator fonts and color palettes across both tracking tiers.

### What's New in v1.0.9
- **Safe & Deterministic Deletion Workflow:**
  - Removed silent post-scan auto-selection. All duplicate items start strictly unchecked upon scan completion to avoid accidental bulk deletion.
  - Batch action buttons (`Delete`, `Trash`, `Move`) start disabled and dynamically activate with real-time file counters (`❌ Delete (1)`, `🗑️ Trash (1)`, `📁 Move (1)`).
  - Confirmation dialogs now provide an explicit, scrollable list of file names and total byte size prior to deletion.
  - Smart selection (`Select Duplicates (Smart)`) is now available on-demand under the `📋 Select ▾` menu.
- **Calibrated Video Similarity Engine:**
  - Replaced single-slice frame extraction with multi-point keyframe sampling across the video timeline (15%, 35%, 50%, 65%, 85%), capturing true visual content rather than adjacent frames.
  - Added automatic black bar detection and cropping (`crop_letterbox`) before 2D DCT calculation, preventing aspect ratio black bars from dominating visual hashes.
  - Introduced GPU tensor duration filter (maximum 8% duration difference for duplicates), preventing videos of different runtimes from matching.
  - Upgraded duplicate clustering to **Strict Mutual Similarity Grouping**, guaranteeing that every item in a group shares direct mutual similarity ≥ threshold.
- **Two-Tier Progress Architecture:**
  - Independent progress tracking for `Current Stage` and cumulative `All Stages` with cyan (`#38bdf8`) and blue (`#0284c7`) styling.
  - Eliminated UI signal flickering during directory crawling. ETA and processing throughput (`it/s`) activate deterministically from Stage 2 onward once exact file counts are known.

### What's New in v1.0.8
- **Deterministic Scan Concurrency Protection (KISS):** Centralized scanning state controller (`_set_scanning_state`). Automatically locks the `Clear Cache` utility button, sidebar scan mode selectors, similarity slider, folder panel, and batch action buttons (`Move`, `Recycle Bin`, `Delete`, `Save`) during active scans, eliminating SQLite lock collisions (`VACUUM` race conditions) and background worker conflicts.
- **Comprehensive Disabled Visual States:** Added full `:disabled` styling for sidebar mode selectors (`sidebarModes`), item highlights, inner divider rules, similarity slider track/handle, and the clear cache button. Inactive controls visually dim into dark/gray tones (`#18181b`, `#222226`, `#52525b`) during scanning, providing instant visual feedback.
- **Fail-Fast Cache Guard Clause:** Added worker thread state verification to `_clear_cache_prompt` and safe exception handling (`try...except`) to guarantee cache maintenance cannot be triggered during an ongoing scan.

### What's New in v1.0.7
- **Redesigned Modern Startup Splash Screen:** Centered, refined and streamlined splash screen architecture with 50% larger typography and high-definition visual scaling (36px title, 18px subtitle, 18px status, 9px progress bar).
- **Pixel-Perfect Vertical Symmetry & Proportions:** Engineered mathematically balanced vertical margins (35px top margin to title, 35px bottom margin to progress bar) and compact card framing (490x250) that eliminates lateral voids.
- **Uncut Font Glyph Rendering:** Added explicit transparent widget backgrounds and ample vertical clearance to eliminate Qt layout compression and preserve full font descenders across all display scale factors.
- **Harmonic Uniform Spacing:** Normalized inter-element spacing to uniform 8px intervals across title, subtitle, status telemetry, and active progress bar.

### What's New in v1.0.6
- **Clean Discovery Indexing Architecture:** Removed fragile crawler estimations that could cause premature 100% progress freezes when scanning folders of varying depths. The filesystem crawler operates in pure discovery mode (`Scanning folders... (X files found)`) with a continuous wave activity indicator.
- **KISS Visual Conciseness:** Replaced rapidly flickering folder/file labels with a static, stable status message (`Scanning folders... (X files found)`), eliminating visual noise while maintaining live discovery counts and transfer speeds (`files/s`).
- **Mathematical Multi-Phase Precision:** Once directory discovery finishes and exact file counts are known, the progress bar deterministically transitions to exact 0% to 100% processing for exact hashing, MobileNetV3 visual embeddings, and NVDEC hardware video decoding.

### What's New in v1.0.5
- **Zero-Layout-Shift Persistent Status Bar:** Engineered permanent two-row telemetry footer with retained geometry (`setRetainSizeWhenHidden(True)`). Eliminates horizontal dancing and vertical layout shifts when starting or completing scans. The full-width progress bar dynamically scales to window dimensions and full-screen maximization without shifting content or tables.
- **Instant Application Startup (<100ms):** PyTorch and CUDA 12 runtimes are lazily imported and verified exclusively within the background `HardwareInitWorker` thread, cutting cold-start launch latency from 12 seconds to under 100 milliseconds and instantly displaying the splash screen.
- **Organic Splash Animation:** Re-architected splash screen progress indicator with dynamic natural acceleration, smoothly advancing while the GPU Gatekeeper validates Ampere/Ada Lovelace/Blackwell hardware.

### What's New in v1.0.4
- **Chunked GPU Matrix Memory Architecture (~40 GB to ~500 MB):** Replaced massive full-matrix calculations with memory-friendly GPU blocks (2,048 rows at a time). Reduced memory footprint from ~40 GB to ~500 MB for 140k+ files, eliminating OS memory swap freezes and checking `stop_check` every 5ms.
- **Deterministic Progress & Instant Estimations:** SQLite-backed file count estimation (`estimate_file_count`) querying recent crawl history and persistent metadata in <1ms without redundant disk I/O, replacing bouncing marquee animations with smooth 0–100% progress bars.
- **Live Indexing Telemetry:** Real-time crawler telemetry reporting active subfolder names, live speed (`files/s`), and continuous ETA calculations.
- **Instant Cancellation Latency (20ms):** Active thread pool future cancellation (`pending.cancel()`) and atomic loops, reducing abort latency from 40s+ to 20ms.
- **Refined Status Feedback:** Clear and accurate completion messages ("Estimated space to free: X MB") with proper singular/plural grammar and explicit "Scan cancelled by user." feedback.

### What's New in v1.0.3
- **High-Throughput Concurrent I/O Engine:** Multi-threaded parallel file reader with 4 MB streaming buffers in `hasher_exact.py`, designed to maximize data transfer rates and eliminate I/O bottlenecks across local NVMe/SATA drives as well as high-speed networked storage (NAS, SAN, iSCSI).
- **Fast Win32 Metadata Crawler:** Directory traversal rewritten using native `os.scandir` caching, eliminating 100% of redundant individual network RPC `stat()` calls over SMB.
- **Instant Scan Cancellation (<0.1s):** Granular atomic checks at 4 MB buffer boundaries and immediate executor queue cancellation (`cancel_futures=True`), reducing scan abort response time from 30 seconds to ~100 milliseconds.
- **In-Process Hardware Binding:** Explicit CUDA Device 0 and NVDEC silicon assignment (`torch.cuda.set_device(0)` and `-hwaccel_device 0`), fully isolated without modifying the Windows registry.
- **Clean Repository Architecture:** Centralized all execution, debug, and build scripts into `scripts/` (`run.bat`, `run_debug.bat`, `build.bat`), keeping root strictly clean.

### Key Features
- **GPU Neural Embeddings:** MobileNetV3 visual embeddings computed directly in VRAM via PyTorch CUDA.
- **Multi-Tier Pipeline:** Size grouping, xxHash64 exact hashing, and perceptual similarity.
- **Hardware NVDEC Acceleration:** High-throughput video decoding using dedicated NVIDIA silicon decoders.
- **Reference Folder Protection:** Protects master directories (`Ref`) from accidental deletion.
- **State & Folder Persistence:** Automatically remembers scan folders and Reference flags in local SQLite database (`data/DupeClean.db`) across sessions.
- **Massive Dataset Resilience:** Industrial-grade SQLite engine with 8KB page sizes, 256MB mmap I/O, strict connection lifecycle management, and automatic 64MB WAL capping, tested and validated on libraries with millions of files without locking or storage limits.
- **100% Portable:** Pre-bundled with Python runtime, CUDA DLLs, and FFmpeg. No installation required.
- **KISS Dark UI:** Clean PySide6 interface with non-blocking real-time telemetry.

### Quick Start (Portable)
1. Download **`DupeClean-v1.1.2-windows-cuda.7z`** below.
2. Extract with [7-Zip](https://www.7-zip.org/) or WinRAR to any folder.
3. Launch **`DupeClean.exe`**.

---

### 📦 Why is the Download ~1.7 GB? (~4.7 GB Unpacked)
Traditional tools are lightweight (~15 MB) because they run basic pixel math on the CPU. **DupeClean bundles a complete GPU AI workstation:**
- Full **NVIDIA CUDA 12 hardware runtimes** (`torch_cuda`, `cublas`, `cudnn`, `cusolver`, `cufft`).
- **PyTorch deep learning vision models** (MobileNetV3).
- Hardware-accelerated **FFmpeg NVDEC** binaries.
- Completely isolated Python 3 runtime - **zero installation or setup required**.

### 💡 Prefer a lightweight download? Run from source!
If you already have Python and an NVIDIA CUDA-enabled PyTorch environment on your machine (e.g., for ComfyUI, Stable Diffusion, or AI development), you don't need to download this 1.7 GB standalone bundle. The repository source code is **under 1 MB**.

Follow the step-by-step setup in the repository documentation:  
👉 **[Running from Source Guide](https://github.com/MarcSant71/DupeClean#-running-from-source-developers--advanced-users)** (includes virtual environment configuration, PyTorch CUDA builds for RTX 30/40/50, and hardware FFmpeg NVDEC binaries).
