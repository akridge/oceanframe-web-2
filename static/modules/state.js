export const state = {
  sessionId: null,
  sourceType: 'video',
  allFrames: [],
  keptIndices: new Set(),
  manualExcludes: new Set(),
  manualIncludes: new Set(),
  tags: {},
  blurThreshold: 20,
  simThreshold: 8,
  brightMin: 0,
  brightMax: 255,
  isAnalyzing: false,
  startTime: 0,
  blurChart: null,
  simChart: null,
  brightChart: null,
  lightboxIndex: 0,
  _blurEdges: [],
  _blurStep: 1,
};

export const batch = {
  queue: [],
  active: -1,
};
