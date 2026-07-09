import {
  canViewBackupJobLogs,
  canViewScriptLogs,
  type BackupPlanRunLogJob,
} from '../../../components/planRunScriptLogs'
import type { BackupPlanRun } from '../../../types'

export function findFirstLogJobForRun(run: BackupPlanRun): BackupPlanRunLogJob | null {
  const scriptExecution = run.script_executions?.find(canViewScriptLogs)
  if (scriptExecution) {
    return {
      id: scriptExecution.id,
      status: scriptExecution.status,
      type: 'script_execution',
      has_logs: scriptExecution.has_logs,
    }
  }

  const repositoryRun = run.repositories.find((candidate) =>
    canViewBackupJobLogs(candidate.backup_job)
  )
  return repositoryRun?.backup_job ?? null
}
