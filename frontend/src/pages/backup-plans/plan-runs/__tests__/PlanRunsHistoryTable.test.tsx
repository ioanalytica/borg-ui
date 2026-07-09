import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { TFunction } from 'i18next'
import { describe, expect, it, vi } from 'vitest'

import type { BackupPlanRun } from '../../../../types'
import { PlanRunsHistoryTable } from '../PlanRunsHistoryTable'

// The component takes `t` as a prop; the shared script section uses the global
// i18n instance, so raw values (script names) render regardless.
const t = ((key: string, opts?: { defaultValue?: string }) =>
  opts?.defaultValue ?? key) as unknown as TFunction

function makeRun(overrides: Partial<BackupPlanRun> = {}): BackupPlanRun {
  return {
    id: 571,
    backup_plan_id: 3,
    trigger: 'manual',
    status: 'completed',
    started_at: '2026-07-09T10:44:00Z',
    completed_at: '2026-07-09T10:49:49Z',
    created_at: '2026-07-09T10:44:00Z',
    repositories: [],
    script_executions: [
      {
        id: 3,
        script_id: null,
        script_name: 'backup-cluster-mariadb',
        hook_type: 'pre-backup',
        status: 'completed',
        started_at: '2026-07-09T10:48:39Z',
        completed_at: '2026-07-09T10:48:41Z',
        execution_time: 2.1,
        exit_code: 0,
        has_logs: true,
      },
    ],
    ...overrides,
  } as BackupPlanRun
}

describe('PlanRunsHistoryTable', () => {
  it('reveals a run’s script executions when its row is expanded', async () => {
    const user = userEvent.setup()
    const onViewLogs = vi.fn()

    render(
      <PlanRunsHistoryTable
        runs={[makeRun()]}
        cancelling={null}
        onViewLogs={onViewLogs}
        onCancel={vi.fn()}
        t={t}
      />
    )

    // Collapsed by default: the script row is not mounted.
    expect(screen.queryByText(/backup-cluster-mariadb/)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /show scripts/i }))

    const scriptRow = await screen.findByText(/backup-cluster-mariadb/)
    expect(scriptRow).toBeInTheDocument()

    // Both the row-level eye and the expanded script row open this run's single
    // script log; clicking the in-row button opens it with the script payload.
    const viewLogsButtons = screen.getAllByRole('button', { name: /view logs/i })
    await user.click(viewLogsButtons[viewLogsButtons.length - 1])
    expect(onViewLogs).toHaveBeenCalledWith({
      id: 3,
      status: 'completed',
      type: 'script_execution',
      has_logs: true,
    })
  })

  it('shows no expand control for runs without script executions', () => {
    render(
      <PlanRunsHistoryTable
        runs={[makeRun({ script_executions: [] })]}
        cancelling={null}
        onViewLogs={vi.fn()}
        onCancel={vi.fn()}
        t={t}
      />
    )

    expect(screen.queryByRole('button', { name: /show scripts/i })).not.toBeInTheDocument()
  })
})
