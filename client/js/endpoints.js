export const scoutEndpoints = {
  health: 'scout/health/',
  jobs: 'scout/jobs/',
  job: (publicId) => `scout/jobs/${publicId}/`,
  explore: (publicId) => `scout/jobs/${publicId}/explore/`,
  run: (publicId) => `scout/runs/${publicId}/`,
  approve: (publicId) => `scout/runs/${publicId}/approve/`,
  start: (publicId) => `scout/runs/${publicId}/start/`,
  pause: (publicId) => `scout/runs/${publicId}/pause/`,
  cancel: (publicId) => `scout/runs/${publicId}/cancel/`,
  records: (publicId) => `scout/runs/${publicId}/records/`,
  exportRun: (publicId) => `scout/runs/${publicId}/export/`,
}

export default scoutEndpoints
