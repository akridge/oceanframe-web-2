
'use strict';

import { initUploadModule, loadSession } from './modules/upload.js';
import { initAnalysisModule, startAnalysis } from './modules/analysis.js';
import { createFilteringModule, initFilterControls, syncBrightLabel } from './modules/filtering.js';
import { createChartsModule } from './modules/charts.js';
import { initPresetModule } from './modules/presets.js';
import { initExportModule } from './modules/export.js';
import { createFrameBrowserModule } from './modules/frame-browser.js';
import { createLightboxModule } from './modules/lightbox.js';
import { createBatchModule } from './modules/batch.js';

const Chart = globalThis.Chart;

let lightboxModule;

const frameBrowser = createFrameBrowserModule({
  openLightbox: frame => lightboxModule?.openLightbox(frame),
});

const { refilter } = createFilteringModule({
  drawTimeline: () => charts.drawTimeline(),
  renderFrameGrid: frameBrowser.renderFrameGrid,
});

const charts = createChartsModule({
  Chart,
  refilter,
  syncBrightLabel,
  openLightbox: frame => lightboxModule?.openLightbox(frame),
});

lightboxModule = createLightboxModule({
  keptFrames: frameBrowser.keptFrames,
  refilter,
  renderFrameGrid: frameBrowser.renderFrameGrid,
});

const analysisDeps = {
  refilter,
  buildHistograms: charts.buildHistograms,
  buildTimeline: charts.buildTimeline,
};

const batchModule = createBatchModule({
  loadSession,
  startAnalysis,
  analysisDeps,
  refilter,
  buildHistograms: charts.buildHistograms,
  syncBrightLabel,
});

initUploadModule({ addFilesToQueue: batchModule.addFilesToQueue });
initAnalysisModule(analysisDeps);
initFilterControls({
  refilter,
  updateBlurLine: charts.updateBlurLine,
  updateSimLine: charts.updateSimLine,
  updateBrightLines: charts.updateBrightLines,
});
initPresetModule({
  refilter,
  syncBrightLabel,
  updateChartLines: () => {
    charts.updateBlurLine();
    charts.updateSimLine();
    charts.updateBrightLines();
  },
});
initExportModule();
frameBrowser.initFrameBrowserControls();
lightboxModule.initLightboxControls();
batchModule.initBatchControls();


