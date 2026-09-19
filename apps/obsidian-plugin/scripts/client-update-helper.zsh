#!/bin/zsh

set -u
set -o pipefail

sc_request=""
sc_transaction_root=""
sc_status=""
sc_stage=""
sc_plugin=""
sc_backup_target=""
sc_mutated=0
sc_active_moved=0
sc_new_active=0
sc_finished=0
sc_obsidian_exited=0
typeset -a sc_duplicate_origins
typeset -a sc_duplicate_targets

sc_cleanup() {
  if [[ -n "${sc_stage:-}" && -d "$sc_stage" ]]; then
    /bin/rm -rf -- "$sc_stage"
  fi
  if [[ "$sc_finished" == "1" && -n "${sc_request:-}" && -f "$sc_request" ]]; then
    /bin/rm -f -- "$sc_request"
  fi
}

sc_write_status() {
  local sc_state="$1"
  local sc_phase="$2"
  local sc_error_code="$3"
  local sc_rolled_back="$4"
  local sc_tmp="$sc_transaction_root/.status.$$"
  local sc_error_json="null"
  if [[ -n "$sc_error_code" ]]; then
    sc_error_json="\"$sc_error_code\""
  fi
  /usr/bin/printf '%s\n' \
    "{\"schema_version\":1,\"transaction_id\":\"$sc_transaction_id\",\"plugin_id\":\"speech-capture\",\"vault_scope_sha256\":\"$sc_vault_scope_sha256\",\"from_version\":\"$sc_from_version\",\"to_version\":\"$sc_to_version\",\"main_sha256\":\"$sc_main_sha256\",\"state\":\"$sc_state\",\"phase\":\"$sc_phase\",\"error_code\":$sc_error_json,\"rolled_back\":$sc_rolled_back}" > "$sc_tmp" || return 1
  /bin/chmod 600 "$sc_tmp" || return 1
  /bin/mv -f "$sc_tmp" "$sc_status"
}

sc_restore() {
  local sc_rolled_back=false
  if [[ "$sc_mutated" == "1" ]]; then
    if [[ "$sc_new_active" == "1" && -n "$sc_plugin" && -d "$sc_plugin" ]]; then
      /bin/rm -rf -- "$sc_plugin"
    fi
    if [[ "$sc_active_moved" == "1" && -n "$sc_backup_target" && -d "$sc_backup_target" ]]; then
      if /bin/mv "$sc_backup_target" "$sc_plugin"; then
        sc_rolled_back=true
      fi
    fi
    local sc_index=${#sc_duplicate_origins[@]}
    while [[ "$sc_index" -gt 0 ]]; do
      local sc_origin="${sc_duplicate_origins[$sc_index]}"
      local sc_target="${sc_duplicate_targets[$sc_index]}"
      if [[ -d "$sc_target" && ! -e "$sc_origin" ]]; then
        /bin/mv "$sc_target" "$sc_origin" 2>/dev/null || true
      fi
      sc_index=$((sc_index - 1))
    done
  fi
  /usr/bin/printf '%s' "$sc_rolled_back"
}

sc_fail() {
  local sc_code="$1"
  local sc_message="$2"
  local sc_rolled_back
  sc_rolled_back="$(sc_restore)"
  if [[ -n "${sc_status:-}" && -n "${sc_transaction_id:-}" ]]; then
    sc_write_status "failed" "failed" "$sc_code" "$sc_rolled_back" 2>/dev/null || true
  fi
  sc_finished=1
  echo "Speech Capture 更新失败 [$sc_code]：$sc_message" >&2
  if [[ "${sc_smoke_test:-0}" != "1" &&
        "${sc_obsidian_exited:-0}" == "1" &&
        "${sc_reopen:-false}" == "true" &&
        -d "${sc_vault:-}" ]]; then
    /usr/bin/open -a Obsidian "$sc_vault" >/dev/null 2>&1 || true
  fi
  exit 1
}

sc_abort() {
  sc_fail "INTERRUPTED" "更新进程被中断。"
}

trap 'sc_cleanup' EXIT
trap 'sc_abort' HUP INT TERM

if [[ "$#" -ne 1 ]]; then
  echo "用法：/bin/zsh $0 /绝对路径/request.json" >&2
  exit 2
fi

sc_request="${1:A}"
if [[ ! -f "$sc_request" || -L "$sc_request" || "${sc_request:t}" != "request.json" ]]; then
  echo "Speech Capture 更新请求不可用。" >&2
  exit 2
fi
sc_transaction_root="${sc_request:h}"
sc_status="$sc_transaction_root/status.json"
if [[ -L "$sc_transaction_root" ]]; then
  echo "Speech Capture 更新目录不安全。" >&2
  exit 2
fi
sc_smoke_test="${SPEECH_CAPTURE_HELPER_SMOKE_TEST:-0}"
if [[ "$sc_smoke_test" == "1" ]]; then
  if [[ "$sc_transaction_root" != /private/tmp/speech-capture-helper-test.*/* ]]; then
    echo "Speech Capture 测试事务目录不安全。" >&2
    exit 2
  fi
else
  sc_expected_update_root="$HOME/Library/Application Support/Speech Capture/Client Updates"
  if [[ "${sc_transaction_root:h}" != "$sc_expected_update_root" ]]; then
    echo "Speech Capture 更新请求不在固定暂存区。" >&2
    exit 2
  fi
fi

sc_extract() {
  /usr/bin/plutil -extract "$1" raw -o - "$sc_request" 2>/dev/null
}

sc_schema_version="$(sc_extract schema_version)" || exit 2
sc_transaction_id="$(sc_extract transaction_id)" || exit 2
sc_plugin_id="$(sc_extract plugin_id)" || exit 2
sc_from_version="$(sc_extract from_version)" || exit 2
sc_to_version="$(sc_extract to_version)" || exit 2
sc_min_app_version="$(sc_extract min_app_version)" || exit 2
sc_vault="$(sc_extract vault_path)" || exit 2
sc_config_name="$(sc_extract config_dir_name)" || exit 2
sc_vault_scope_sha256="$(sc_extract vault_scope_sha256)" || exit 2
sc_archive="$(sc_extract archive_path)" || exit 2
sc_archive_sha256="$(sc_extract archive_sha256)" || exit 2
sc_main_sha256="$(sc_extract main_sha256)" || exit 2
sc_current_main_sha256="$(sc_extract current_main_sha256)" || exit 2
sc_reopen="$(sc_extract reopen)" || exit 2

sc_request_keys="$(/usr/bin/plutil -p "$sc_request" 2>/dev/null | /usr/bin/sed -n 's/^  "\([^"]*\)" =>.*/\1/p' | /usr/bin/sort)"
sc_expected_request_keys=$'archive_path\narchive_sha256\nconfig_dir_name\ncurrent_main_sha256\nfrom_version\nmain_sha256\nmin_app_version\nplugin_id\nreopen\nschema_version\nto_version\ntransaction_id\nvault_path\nvault_scope_sha256'
if [[ "$sc_request_keys" != "$sc_expected_request_keys" ]]; then
  exit 2
fi

if [[ "$sc_schema_version" != "1" || "$sc_plugin_id" != "speech-capture" ]]; then
  exit 2
fi
if [[ ! "$sc_transaction_id" =~ '^update_[0-9a-f]{32}$' || "${sc_transaction_root:t}" != "$sc_transaction_id" ]]; then
  exit 2
fi
if [[ ! "$sc_from_version" =~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' ||
      ! "$sc_to_version" =~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' ||
      ! "$sc_min_app_version" =~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' ]]; then
  exit 2
fi
if [[ ! "$sc_archive_sha256" =~ '^[0-9a-f]{64}$' ||
      ! "$sc_main_sha256" =~ '^[0-9a-f]{64}$' ||
      ! "$sc_current_main_sha256" =~ '^[0-9a-f]{64}$' ||
      ! "$sc_vault_scope_sha256" =~ '^[0-9a-f]{64}$' ]]; then
  exit 2
fi
if [[ "$sc_config_name" == "." || "$sc_config_name" == ".." || ! "$sc_config_name" =~ '^[A-Za-z0-9._-]{1,80}$' ]]; then
  exit 2
fi
if [[ "$sc_reopen" != "true" && "$sc_reopen" != "false" ]]; then
  exit 2
fi
autoload -Uz is-at-least
if [[ "$sc_to_version" == "$sc_from_version" ]] || ! is-at-least "$sc_from_version" "$sc_to_version"; then
  exit 2
fi
if [[ "$sc_vault" == *$'\n'* || "$sc_archive" == *$'\n'* ]]; then
  exit 2
fi

sc_vault="${sc_vault:A}"
sc_archive="${sc_archive:A}"
sc_actual_vault_scope_sha256="$(
  /usr/bin/printf '%s\0%s' "$sc_vault" "$sc_config_name" |
    /usr/bin/shasum -a 256 |
    /usr/bin/awk '{print $1}'
)"
if [[ "$sc_actual_vault_scope_sha256" != "$sc_vault_scope_sha256" ]]; then
  sc_fail "VAULT_SCOPE_MISMATCH" "请求中的 Vault 标识不匹配。"
fi
if [[ "$sc_archive" != "$sc_transaction_root/speech-capture-$sc_to_version-alpha.zip" ||
      ! -f "$sc_archive" || -L "$sc_archive" ]]; then
  sc_fail "ARCHIVE_UNAVAILABLE" "候选安装包不存在或路径不受信任。"
fi

sc_config="$sc_vault/$sc_config_name"
sc_plugins="$sc_config/plugins"
sc_plugin="$sc_plugins/speech-capture"
sc_backup_root="$sc_config/plugin-backups"
if [[ ! -d "$sc_vault" || ! -d "$sc_config" || ! -d "$sc_plugin" || -L "$sc_plugin" ]]; then
  sc_fail "VAULT_MISMATCH" "请求中的 Vault 或活动插件目录不匹配。"
fi
if [[ ! -f "$sc_plugin/manifest.json" || -L "$sc_plugin/manifest.json" ||
      ! -f "$sc_plugin/main.js" || -L "$sc_plugin/main.js" ]]; then
  sc_fail "ACTIVE_PLUGIN_INVALID" "当前插件文件不完整或不安全。"
fi
if [[ -e "$sc_plugin/data.json" && ( ! -f "$sc_plugin/data.json" || -L "$sc_plugin/data.json" ) ]]; then
  sc_fail "ACTIVE_DATA_INVALID" "当前 data.json 不是可安全保留的普通文件。"
fi

sc_active_id="$(/usr/bin/plutil -extract id raw -o - "$sc_plugin/manifest.json" 2>/dev/null)"
sc_active_version="$(/usr/bin/plutil -extract version raw -o - "$sc_plugin/manifest.json" 2>/dev/null)"
sc_active_main_sha256="$(/usr/bin/shasum -a 256 "$sc_plugin/main.js" | /usr/bin/awk '{print $1}')"
if [[ "$sc_active_id" != "speech-capture" || "$sc_active_version" != "$sc_from_version" ||
      "$sc_active_main_sha256" != "$sc_current_main_sha256" ]]; then
  sc_fail "ACTIVE_PLUGIN_CHANGED" "活动插件在确认后发生了变化。"
fi

if [[ "$sc_smoke_test" != "1" ]]; then
  sc_write_status "waiting_for_exit" "waiting_for_exit" "" false || \
    sc_fail "STATUS_WRITE_FAILED" "无法记录等待退出状态。"
  sc_wait_count=0
  while /usr/bin/pgrep -x Obsidian >/dev/null 2>&1; do
    if [[ "$sc_wait_count" -ge 600 ]]; then
      sc_fail "OBSIDIAN_EXIT_TIMEOUT" "等待 Obsidian 退出超时。"
    fi
    /bin/sleep 1
    sc_wait_count=$((sc_wait_count + 1))
  done
  sc_obsidian_exited=1
fi

sc_write_status "applying" "validating" "" false || \
  sc_fail "STATUS_WRITE_FAILED" "无法记录校验状态。"
sc_actual_archive_sha256="$(/usr/bin/shasum -a 256 "$sc_archive" | /usr/bin/awk '{print $1}')"
if [[ "$sc_actual_archive_sha256" != "$sc_archive_sha256" ]]; then
  sc_fail "ARCHIVE_HASH_MISMATCH" "候选安装包 SHA-256 校验失败。"
fi

sc_archive_size="$(/usr/bin/stat -f '%z' "$sc_archive" 2>/dev/null)"
if [[ "$sc_smoke_test" == "1" && "${SPEECH_CAPTURE_HELPER_FORCE_NO_SPACE:-0}" == "1" ]]; then
  sc_free_kb=0
else
  sc_free_kb="$(/bin/df -k "$sc_config" | /usr/bin/awk 'NR == 2 {print $4}')"
fi
if [[ ! "$sc_archive_size" =~ '^[0-9]+$' || ! "$sc_free_kb" =~ '^[0-9]+$' ||
      "$((sc_free_kb * 1024))" -lt "$((sc_archive_size * 4 + 5242880))" ]]; then
  sc_fail "INSUFFICIENT_SPACE" "可用空间不足，尚未修改活动插件。"
fi

sc_entries="$(/usr/bin/unzip -Z1 "$sc_archive" 2>/dev/null | /usr/bin/sort)" || \
  sc_fail "ARCHIVE_INVALID" "无法读取候选安装包。"
sc_expected_entries=$'speech-capture/main.js\nspeech-capture/manifest.json\nspeech-capture/styles.css'
if [[ "$sc_entries" != "$sc_expected_entries" ]]; then
  sc_fail "ARCHIVE_ENTRIES_INVALID" "候选安装包文件列表不符合白名单。"
fi

/bin/mkdir -p "$sc_plugins" "$sc_backup_root" || \
  sc_fail "DIRECTORY_CREATE_FAILED" "无法创建插件或备份目录。"
sc_stage="$(/usr/bin/mktemp -d "$sc_config/.speech-capture-installing.XXXXXX")" || \
  sc_fail "STAGING_CREATE_FAILED" "无法创建同卷安装暂存目录。"
/usr/bin/ditto -x -k "$sc_archive" "$sc_stage/unpacked" || \
  sc_fail "ARCHIVE_EXTRACT_FAILED" "无法解压候选安装包。"

sc_source="$sc_stage/unpacked/speech-capture"
for sc_file in main.js manifest.json styles.css; do
  if [[ ! -f "$sc_source/$sc_file" || -L "$sc_source/$sc_file" ]]; then
    sc_fail "PACKAGE_FILE_INVALID" "候选安装包包含缺失或不安全文件。"
  fi
done
sc_package_id="$(/usr/bin/plutil -extract id raw -o - "$sc_source/manifest.json" 2>/dev/null)"
sc_package_version="$(/usr/bin/plutil -extract version raw -o - "$sc_source/manifest.json" 2>/dev/null)"
sc_package_min_app="$(/usr/bin/plutil -extract minAppVersion raw -o - "$sc_source/manifest.json" 2>/dev/null)"
sc_package_desktop="$(/usr/bin/plutil -extract isDesktopOnly raw -o - "$sc_source/manifest.json" 2>/dev/null)"
sc_package_main_sha256="$(/usr/bin/shasum -a 256 "$sc_source/main.js" | /usr/bin/awk '{print $1}')"
if [[ "$sc_package_id" != "speech-capture" || "$sc_package_version" != "$sc_to_version" ||
      "$sc_package_min_app" != "$sc_min_app_version" || "$sc_package_desktop" != "true" ||
      "$sc_package_main_sha256" != "$sc_main_sha256" ]]; then
  sc_fail "PACKAGE_IDENTITY_INVALID" "候选插件身份、版本或主文件校验失败。"
fi

sc_ready="$sc_stage/ready"
/bin/mkdir "$sc_ready" || sc_fail "STAGING_CREATE_FAILED" "无法准备候选插件目录。"
for sc_file in main.js manifest.json styles.css; do
  /usr/bin/ditto "$sc_source/$sc_file" "$sc_ready/$sc_file" || \
    sc_fail "STAGING_COPY_FAILED" "无法准备候选插件文件。"
done
if [[ -f "$sc_plugin/data.json" ]]; then
  /usr/bin/ditto "$sc_plugin/data.json" "$sc_ready/data.json" || \
    sc_fail "DATA_PRESERVE_FAILED" "无法保留现有插件设置。"
fi

sc_write_status "applying" "replacing" "" false || \
  sc_fail "STATUS_WRITE_FAILED" "无法记录替换状态。"
sc_timestamp="$(/bin/date '+%Y%m%d-%H%M%S')-$$"
sc_duplicate_count=0
while IFS= read -r -d '' sc_other_manifest; do
  sc_other_dir="${sc_other_manifest:h}"
  if [[ "$sc_other_dir" == "$sc_plugin" ]]; then
    continue
  fi
  sc_other_id="$(/usr/bin/plutil -extract id raw -o - "$sc_other_manifest" 2>/dev/null)"
  if [[ "$sc_other_id" == "speech-capture" ]]; then
    sc_duplicate_target="$sc_backup_root/${sc_other_dir:t}.duplicate-$sc_timestamp-$sc_duplicate_count"
    /bin/mv "$sc_other_dir" "$sc_duplicate_target" || \
      sc_fail "DUPLICATE_MOVE_FAILED" "无法把重复插件移出加载目录。"
    sc_duplicate_origins+=("$sc_other_dir")
    sc_duplicate_targets+=("$sc_duplicate_target")
    sc_mutated=1
    sc_duplicate_count=$((sc_duplicate_count + 1))
  fi
done < <(/usr/bin/find "$sc_plugins" -mindepth 2 -maxdepth 2 -name manifest.json -print0)

if [[ "$sc_smoke_test" == "1" && "${SPEECH_CAPTURE_HELPER_FAIL_AFTER_DUPLICATES:-0}" == "1" ]]; then
  sc_fail "SYNTHETIC_AFTER_DUPLICATES_FAILURE" "合成的重复目录迁移后故障。"
fi

sc_backup_target="$sc_backup_root/speech-capture-$sc_timestamp"
/bin/mv "$sc_plugin" "$sc_backup_target" || \
  sc_fail "ACTIVE_BACKUP_FAILED" "无法备份当前插件目录。"
sc_mutated=1
sc_active_moved=1

if [[ "$sc_smoke_test" == "1" && "${SPEECH_CAPTURE_HELPER_FAIL_AFTER_BACKUP:-0}" == "1" ]]; then
  sc_fail "SYNTHETIC_AFTER_BACKUP_FAILURE" "合成的备份后故障。"
fi

/bin/mv "$sc_ready" "$sc_plugin" || \
  sc_fail "ACTIVE_REPLACE_FAILED" "无法原子启用候选插件。"
sc_new_active=1

sc_installed_version="$(/usr/bin/plutil -extract version raw -o - "$sc_plugin/manifest.json" 2>/dev/null)"
sc_installed_main_sha256="$(/usr/bin/shasum -a 256 "$sc_plugin/main.js" | /usr/bin/awk '{print $1}')"
if [[ "$sc_installed_version" != "$sc_to_version" || "$sc_installed_main_sha256" != "$sc_main_sha256" ]]; then
  sc_fail "POST_INSTALL_VERIFY_FAILED" "安装后版本或 main.js 校验失败。"
fi

sc_write_status "restart_required" "restart_required" "" false || \
  sc_fail "STATUS_WRITE_FAILED" "无法记录等待重启状态。"
sc_finished=1
echo "Speech Capture $sc_to_version 已安装到明确指定的 Vault，并通过磁盘校验。"
echo "请在 Obsidian 重开后确认实际加载版本。"

if [[ "$sc_smoke_test" != "1" && "$sc_reopen" == "true" ]]; then
  /usr/bin/open -a Obsidian "$sc_vault" >/dev/null 2>&1 || true
fi

exit 0
