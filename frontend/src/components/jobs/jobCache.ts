// The lists that show deletable job rows, by query-key prefix. Each page
// keys its query by its own filters, so these match every variant.
export const JOB_LIST_QUERY_KEYS = [
  ['activity'],
  ['backup-status-manual'],
  ['backup-jobs-all'],
  ['scheduled-check-history'],
] as const

interface JobRef {
  id: string | number
  type?: string
}

// Keyed by type and id together: a script execution and an operation can
// share an id. Rows without a type come from single-type lists of backups.
const isSameJob = (row: Record<string, unknown>, job: JobRef) =>
  String(row.id) === String(job.id) && (row.type || 'backup') === (job.type || 'backup')

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null

/**
 * Return cached list data without the given job, in whichever shape the list
 * caches: a plain array, `{ jobs }`, an axios response around either, or an
 * infinite query's `{ pages }`. Follow-up steps nested under a row are
 * filtered too. Data of any other shape comes back unchanged.
 */
export function withoutJob(data: unknown, job: JobRef): unknown {
  if (Array.isArray(data)) {
    return data
      .filter((row) => !(isObject(row) && isSameJob(row, job)))
      .map((row) =>
        isObject(row) && Array.isArray(row.followups)
          ? { ...row, followups: withoutJob(row.followups, job) }
          : row
      )
  }
  if (!isObject(data)) return data
  if (Array.isArray(data.pages)) {
    return { ...data, pages: data.pages.map((page) => withoutJob(page, job)) }
  }
  if (Array.isArray(data.jobs)) {
    return { ...data, jobs: withoutJob(data.jobs, job) }
  }
  if (isObject(data.data)) {
    return { ...data, data: withoutJob(data.data, job) }
  }
  return data
}
