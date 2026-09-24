import { describe, expect, it } from 'vitest'
import { withoutJob } from '../jobCache'

const backup7 = { id: 7, type: 'backup', status: 'completed' }
const backup8 = { id: 8, type: 'backup', status: 'completed' }
const script7 = { id: 7, type: 'script_execution', status: 'completed' }
const planRun7 = { id: 7, type: 'backup_plan_run', status: 'failed' }

describe('withoutJob', () => {
  it('filters a plain array by type and id together', () => {
    const rows = [backup7, script7, planRun7, backup8]
    expect(withoutJob(rows, { id: 7, type: 'backup' })).toEqual([script7, planRun7, backup8])
    expect(withoutJob(rows, { id: 7, type: 'backup_plan_run' })).toEqual([
      backup7,
      script7,
      backup8,
    ])
  })

  it('reads a missing type as a backup, as the delete call does', () => {
    const untyped = [{ id: 7 }, { id: 8 }]
    expect(withoutJob(untyped, { id: 7 })).toEqual([{ id: 8 }])
    expect(withoutJob(untyped, { id: 7, type: 'check' })).toEqual(untyped)
  })

  it('matches ids across string and number', () => {
    expect(withoutJob([backup7, backup8], { id: '7', type: 'backup' })).toEqual([backup8])
  })

  it('filters every page of an infinite query and keeps its page params', () => {
    const data = { pages: [[backup8], [backup7, script7]], pageParams: [null, 'cursor'] }
    expect(withoutJob(data, { id: 7, type: 'backup' })).toEqual({
      pages: [[backup8], [script7]],
      pageParams: [null, 'cursor'],
    })
  })

  it('filters { jobs } and an axios response around it', () => {
    expect(withoutJob({ jobs: [backup7, backup8], total: 2 }, backup7)).toEqual({
      jobs: [backup8],
      total: 2,
    })
    expect(withoutJob({ status: 200, data: { jobs: [backup7, backup8] } }, backup7)).toEqual({
      status: 200,
      data: { jobs: [backup8] },
    })
  })

  it('drops a follow-up step nested under a row', () => {
    const run = { ...backup8, followups: [{ id: 3, type: 'prune' }, script7] }
    expect(withoutJob({ pages: [[run]] }, { id: 3, type: 'prune' })).toEqual({
      pages: [[{ ...backup8, followups: [script7] }]],
    })
  })

  it('leaves data of any other shape alone', () => {
    const other = { cells: [backup7] }
    expect(withoutJob(other, backup7)).toBe(other)
    expect(withoutJob(undefined, backup7)).toBeUndefined()
  })
})
