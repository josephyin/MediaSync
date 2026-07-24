export interface CloudAccount {
  id: number
  provider: string
  name: string
  account_identity: string | null
  provider_user_id: string | null
  default_drive_id: string | null
  status: string
  last_verified_at: string | null
  last_error: string | null
  open_auth_mode: 'alistgo' | 'openlist' | 'custom' | null
  open_account_identity: string | null
  open_status: 'pending' | 'active' | 'error' | null
  open_last_verified_at: string | null
  open_last_error: string | null
  open_token_url: string | null
  open_client_id: string | null
  created_at: string
}

export interface DriveInfo {
  id: string
  name: string
  type: 'default' | 'resource' | 'backup' | 'custom'
}

export interface FolderItem {
  id: string
  name: string
  type: string
  size: number | null
  updated_at: string | null
}

export interface Subscription {
  id: number
  cloud_account_id: number
  name: string
  provider: string
  share_url: string
  target_path: string
  target_drive_id: string | null
  target_drive_type: 'default' | 'resource' | 'backup' | 'custom' | null
  schedule: string
  enabled: boolean
  status: string
  initial_sync_mode: string
  last_scanned_at: string | null
  last_full_scanned_at: string | null
  next_scan_at: string | null
  last_error: string | null
  created_at: string
  updated_at: string
}

export interface CloudFile {
  id: number
  subscription_id: number
  filename: string
  relative_path: string
  size: number | null
  status: string
  target_path: string | null
  saved_at: string | null
  last_error: string | null
  first_seen_at: string
  last_seen_at: string
  created_at: string
}

export interface Task {
  id: number
  subscription_id: number | null
  file_id: number | null
  type: string
  trigger_type: string
  status: string
  message: string | null
  error_code: string | null
  attempt_count: number
  max_attempts: number
  created_at: string
  started_at: string | null
  finished_at: string | null
  next_attempt_at: string | null
}
