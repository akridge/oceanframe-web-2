import { state } from './state.js';
import {
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
  paramsSec,
  framesSec,
  exportSec,
  progressSec,
} from './dom.js';
import { $, extOf, hideSection, showSection } from './utils.js';
import { authFetch } from '../auth/session.js';

let videoBlobUrl = null;

export function initUploadModule({ addFilesToQueue }) {
  browseVideoBtn.addEventListener('click', e => {
    e.stopPropagation();
    fileInputVideo.click();
  });
  browseImagesBtn.addEventListener('click', e => {
    e.stopPropagation();
    fileInputImages.click();
  });
  addToQueueBtn.addEventListener('click', () => fileInputQueue.click());
  clearMediaBtn.addEventListener('click', e => {
    e.stopPropagation();
    clearMedia();
  });

  dropZone.addEventListener('click', () => {
    if (dropZone.classList.contains('loaded')) return;
    fileInputVideo.click();
  });
  dropZone.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      fileInputVideo.click();
    }
  });

  fileInputVideo.addEventListener('change', () => {
    if (fileInputVideo.files[0]) handleVideoFile(fileInputVideo.files[0]);
  });
  fileInputImages.addEventListener('change', () => {
    if (fileInputImages.files.length) handleImageFiles([...fileInputImages.files]);
  });
  fileInputQueue.addEventListener('change', () => {
    if (fileInputQueue.files.length) addFilesToQueue([...fileInputQueue.files]);
  });

  dropZone.addEventListener('dragover', e => {
    e.preventDefault();
    dropZone.classList.add('drag-over');
  });
  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
  dropZone.addEventListener('drop', e => {
    e.preventDefault();
    dropZone.classList.remove('drag-over');
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;

    const IMAGE_EXTS = new Set(['.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.webp']);
    const VIDEO_EXTS = new Set(['.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.m4v']);

    const videos = files.filter(f => VIDEO_EXTS.has(extOf(f)));
    const images = files.filter(f => IMAGE_EXTS.has(extOf(f)));

    if (videos.length > 1) {
      addFilesToQueue(videos);
    } else if (videos.length === 1) {
      handleVideoFile(videos[0]);
      if (videos.length > 1) addFilesToQueue(videos.slice(1));
    } else if (images.length > 0) {
      handleImageFiles(images);
    }
  });
}

export function loadSession(sessionId, sourceType, meta) {
  state.sessionId = sessionId;
  state.sourceType = sourceType;
  state.allFrames = [];
  state.keptIndices = new Set();
  state.manualExcludes.clear();
  state.manualIncludes.clear();
  state.tags = {};

  hideSection(paramsSec);
  hideSection(framesSec);
  hideSection(exportSec);
  hideSection(progressSec);

  populateMeta(meta);
  showSection(fileMeta);
  showSection(analysisCtrl);
  startBtn.disabled = false;
}

export function clearMedia() {
  resetDropZone();
  hideSection(fileMeta);
  hideSection(analysisCtrl);
  hideSection(paramsSec);
  hideSection(framesSec);
  hideSection(exportSec);
  hideSection(progressSec);

  state.allFrames = [];
  state.keptIndices = new Set();
  state.manualExcludes.clear();
  state.manualIncludes.clear();
  state.tags = {};
  state.isAnalyzing = false;

  if (videoBlobUrl) {
    URL.revokeObjectURL(videoBlobUrl);
    videoBlobUrl = null;
  }
  modeGroup.style.display = '';
  fileInputVideo.value = '';
  fileInputImages.value = '';
}

function resetDropZone() {
  videoPreview.classList.add('hidden');
  videoPreview.src = '';
  imagesPreview.classList.add('hidden');
  imagesPreview.innerHTML = '';
  clearMediaBtn.classList.add('hidden');
  dropZoneInner.classList.remove('hidden');
  dropZone.classList.remove('loaded');
  state.sessionId = null;
}

function showDropError(msg) {
  setDropZoneBusy(false);
  dropZoneInner.querySelector('.drop-primary').textContent = msg;
  dropZoneInner.querySelector('.drop-primary').style.color = 'var(--error)';
}

function setDropZoneBusy(isBusy, label = 'Uploading media...') {
  dropZone.classList.toggle('busy', isBusy);
  dropZoneBusy.classList.toggle('hidden', !isBusy);
  dropZoneBusyText.textContent = label;
}

function populateMeta(m) {
  $('meta-file').textContent = m.filename;
  $('meta-fmt').textContent = m.codec || '—';
  $('meta-size').textContent = m.file_size_str;
  $('meta-dur').textContent = m.duration_str;
  $('meta-fps').textContent = m.fps + (m.codec === 'images' ? ' img/sec' : ' fps');
  $('meta-res').textContent = m.width && m.height ? `${m.width} × ${m.height}` : '—';
  $('meta-frames').textContent = m.frame_count.toLocaleString();
}

async function handleVideoFile(file) {
  resetDropZone();
  dropZoneInner.querySelector('.drop-primary').textContent = 'Uploading…';
  setDropZoneBusy(true, 'Uploading video...');

  const fd = new FormData();
  fd.append('file', file);

  let res;
  let data;
  try {
    res = await authFetch('/api/upload', { method: 'POST', body: fd });
    data = await res.json();
  } catch (err) {
    showDropError('Upload failed: ' + err.message);
    return;
  } finally {
    setDropZoneBusy(false);
  }
  if (!res.ok) {
    showDropError(data.detail || 'Upload error');
    return;
  }

  loadSession(data.session_id, 'video', data.meta);

  if (videoBlobUrl) URL.revokeObjectURL(videoBlobUrl);
  videoBlobUrl = URL.createObjectURL(file);
  videoPreview.src = videoBlobUrl;
  dropZoneInner.classList.add('hidden');
  videoPreview.classList.remove('hidden');
  clearMediaBtn.classList.remove('hidden');
  dropZone.classList.add('loaded');
  modeGroup.style.display = '';
}

async function handleImageFiles(files) {
  resetDropZone();
  dropZoneInner.querySelector('.drop-primary').textContent = `Uploading ${files.length} images…`;
  setDropZoneBusy(true, `Uploading ${files.length} images...`);

  const fd = new FormData();
  for (const f of files) fd.append('files', f);

  let res;
  let data;
  try {
    res = await authFetch('/api/upload-images', { method: 'POST', body: fd });
    data = await res.json();
  } catch (err) {
    showDropError('Upload failed: ' + err.message);
    return;
  } finally {
    setDropZoneBusy(false);
  }
  if (!res.ok) {
    showDropError(data.detail || 'Upload error');
    return;
  }

  loadSession(data.session_id, 'images', data.meta);

  dropZoneInner.classList.add('hidden');
  imagesPreview.classList.remove('hidden');
  clearMediaBtn.classList.remove('hidden');
  imagesPreview.innerHTML = '';

  const preview = files.slice(0, 20);
  for (const f of preview) {
    const img = document.createElement('img');
    const url = URL.createObjectURL(f);
    img.src = url;
    img.alt = f.name;
    img.onload = () => URL.revokeObjectURL(url);
    imagesPreview.appendChild(img);
  }

  if (files.length > 20) {
    const more = document.createElement('div');
    more.className = 'img-count';
    more.textContent = `+${files.length - 20}`;
    imagesPreview.appendChild(more);
  }

  dropZone.classList.add('loaded');
  modeGroup.style.display = 'none';
}
