import { $ } from './utils.js';

const dropZone = $('drop-zone');
const dropZoneInner = $('drop-zone-inner');
const dropZoneBusy = $('drop-zone-busy');
const dropZoneBusyText = $('drop-zone-busy-text');
const videoPreview = $('video-preview');
const imagesPreview = $('images-preview');
const fileInputVideo = $('file-input-video');
const fileInputImages = $('file-input-images');
const fileInputQueue = $('file-input-queue');
const browseVideoBtn = $('browse-video-btn');
const browseImagesBtn = $('browse-images-btn');
const addToQueueBtn = $('add-to-queue-btn');
const clearMediaBtn = $('clear-media-btn');
const fileMeta = $('file-meta');
const analysisCtrl = $('analysis-controls');
const modeGroup = $('mode-group');
const startBtn = $('start-btn');

const progressSec = $('progress-section');
const progressBar = $('progress-bar');
const progressLbl = $('progress-label');
const progressCnt = $('progress-count');
const progressPct = $('progress-pct');
const progressTime = $('progress-time');
const cancelBtn = $('cancel-btn');

const paramsSec = $('parameters-section');
const framesSec = $('frames-section');
const exportSec = $('export-section');

const blurSlider = $('blur-slider');
const blurVal = $('blur-val');
const simSlider = $('sim-slider');
const simVal = $('sim-val');
const brightMinSlider = $('bright-min-slider');
const brightMaxSlider = $('bright-max-slider');
const brightRangeVal = $('bright-range-val');
const keptCountEl = $('kept-count');

const frameGrid = $('frame-grid');
const framesEmpty = $('frames-empty');
const sortSelect = $('sort-select');
const sizeBtns = document.querySelectorAll('.size-btn');

const qualitySlider = $('quality-slider');
const qualityVal = $('quality-val');
const resSelect = $('res-select');
const exifCheckbox = $('exif-checkbox');
const downloadZipBtn = $('download-zip-btn');
const downloadCsvBtn = $('download-csv-btn');
const zipFrameCount = $('zip-frame-count');

const lightbox = $('lightbox');
const lightboxBg = $('lightbox-backdrop');
const lightboxImg = $('lightbox-img');
const lightboxClose = $('lightbox-close');
const lightboxPrev = $('lightbox-prev');
const lightboxNext = $('lightbox-next');
const lbIndex = $('lb-index');
const lbTs = $('lb-ts');
const lbBlur = $('lb-blur');
const lbBright = $('lb-bright');
const lbCast = $('lb-cast');
const lbHash = $('lb-hash');
const lbToggleBtn = $('lb-toggle-btn');
const tagInput = $('tag-input');
const tagAddBtn = $('tag-add-btn');
const tagChips = $('tag-chips');

const presetSelect = $('preset-select');
const presetNameInput = $('preset-name');
const presetSaveBtn = $('preset-save-btn');
const presetDeleteBtn = $('preset-delete-btn');

const sessionSwitcher = $('session-switcher');
const sessionSelect = $('session-select');

const batchQueue = $('batch-queue');
const batchList = $('batch-list');

export {
  $,
  dropZone,
  dropZoneInner,
  dropZoneBusy,
  dropZoneBusyText,
  videoPreview,
  imagesPreview,
  fileInputVideo,
  fileInputImages,
  fileInputQueue,
  browseVideoBtn,
  browseImagesBtn,
  addToQueueBtn,
  clearMediaBtn,
  fileMeta,
  analysisCtrl,
  modeGroup,
  startBtn,
  progressSec,
  progressBar,
  progressLbl,
  progressCnt,
  progressPct,
  progressTime,
  cancelBtn,
  paramsSec,
  framesSec,
  exportSec,
  blurSlider,
  blurVal,
  simSlider,
  simVal,
  brightMinSlider,
  brightMaxSlider,
  brightRangeVal,
  keptCountEl,
  frameGrid,
  framesEmpty,
  sortSelect,
  sizeBtns,
  qualitySlider,
  qualityVal,
  resSelect,
  exifCheckbox,
  downloadZipBtn,
  downloadCsvBtn,
  zipFrameCount,
  lightbox,
  lightboxBg,
  lightboxImg,
  lightboxClose,
  lightboxPrev,
  lightboxNext,
  lbIndex,
  lbTs,
  lbBlur,
  lbBright,
  lbCast,
  lbHash,
  lbToggleBtn,
  tagInput,
  tagAddBtn,
  tagChips,
  presetSelect,
  presetNameInput,
  presetSaveBtn,
  presetDeleteBtn,
  sessionSwitcher,
  sessionSelect,
  batchQueue,
  batchList,
};
