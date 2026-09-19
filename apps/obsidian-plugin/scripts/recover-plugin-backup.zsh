#!/bin/zsh

set -u
set -o pipefail

sc_failed_active=""
sc_restore_started=0
sc_restore_completed=0

sc_fail() {
  echo
  echo "恢复失败：$1" >&2
  exit 1
}

sc_rollback() {
  if [[ "$sc_restore_started" == "1" && "$sc_restore_completed" != "1" ]]; then
    if [[ -d "${sc_plugin:-}" && ! -e "${sc_backup:-}" ]]; then
      /bin/mv "$sc_plugin" "$sc_backup" 2>/dev/null || true
    fi
    if [[ -n "$sc_failed_active" && -d "$sc_failed_active" && ! -e "${sc_plugin:-}" ]]; then
      /bin/mv "$sc_failed_active" "$sc_plugin" 2>/dev/null || true
    fi
  fi
}

trap 'sc_rollback' EXIT
trap 'sc_fail "恢复进程被中断。"' HUP INT TERM

if [[ "$#" -ne 2 ]]; then
  echo "用法："
  echo "/bin/zsh \"$0\" \"/完整/Vault/路径\" \"speech-capture-备份目录名\""
  echo
  echo "脚本不会猜测 Vault 或备份。备份目录名必须来自该 Vault 的 .obsidian/plugin-backups。"
  exit 2
fi

sc_vault="${1:A}"
sc_backup_name="$2"
sc_config="$sc_vault/.obsidian"
sc_plugins="$sc_config/plugins"
sc_plugin="$sc_plugins/speech-capture"
sc_backup_root="$sc_config/plugin-backups"
sc_backup="$sc_backup_root/$sc_backup_name"
sc_smoke_test="${SPEECH_CAPTURE_RECOVERY_SMOKE_TEST:-0}"

if [[ "$sc_smoke_test" == "1" && "$sc_vault" != /private/tmp/speech-capture-recovery-test.*/* ]]; then
  sc_fail "恢复演练标记只能用于 /private/tmp 下的测试 Vault。"
fi
if [[ "$sc_smoke_test" != "1" ]] && /usr/bin/pgrep -x Obsidian >/dev/null 2>&1; then
  sc_fail "Obsidian 仍在运行。请先按 Command + Q 完全退出，再重新运行本命令。"
fi
if [[ ! -d "$sc_vault" || ! -d "$sc_config" || ! -d "$sc_plugins" || ! -d "$sc_backup_root" ]]; then
  sc_fail "指定路径不是包含插件备份的有效 Obsidian Vault。"
fi
if [[ ! "$sc_backup_name" =~ '^speech-capture-[A-Za-z0-9._-]{1,160}$' ]]; then
  sc_fail "备份目录名不符合 Speech Capture 备份规则。"
fi
if [[ "${sc_backup:A:h}" != "${sc_backup_root:A}" || ! -d "$sc_backup" || -L "$sc_backup" ]]; then
  sc_fail "明确指定的备份目录不存在或不安全。"
fi
if [[ ! -f "$sc_backup/manifest.json" || -L "$sc_backup/manifest.json" ||
      ! -f "$sc_backup/main.js" || -L "$sc_backup/main.js" ||
      ! -f "$sc_backup/styles.css" || -L "$sc_backup/styles.css" ]]; then
  sc_fail "备份插件文件不完整或不安全。"
fi
if [[ -L "$sc_backup/data.json" ||
      ( -e "$sc_backup/data.json" && ! -f "$sc_backup/data.json" ) ]]; then
  sc_fail "备份中的 data.json 不是可安全恢复的普通文件。"
fi

sc_backup_id="$(/usr/bin/plutil -extract id raw -o - "$sc_backup/manifest.json" 2>/dev/null)"
sc_backup_version="$(/usr/bin/plutil -extract version raw -o - "$sc_backup/manifest.json" 2>/dev/null)"
if [[ "$sc_backup_id" != "speech-capture" ||
      ! "$sc_backup_version" =~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' ]]; then
  sc_fail "备份插件身份或版本无效。"
fi

if [[ -e "$sc_plugin" && ( ! -d "$sc_plugin" || -L "$sc_plugin" ) ]]; then
  sc_fail "当前活动插件路径不是可安全替换的目录。"
fi
if [[ -d "$sc_plugin" && -f "$sc_plugin/manifest.json" ]]; then
  sc_active_id="$(/usr/bin/plutil -extract id raw -o - "$sc_plugin/manifest.json" 2>/dev/null)"
  if [[ "$sc_active_id" != "speech-capture" ]]; then
    sc_fail "当前活动目录不属于 Speech Capture。"
  fi
fi

sc_timestamp="$(/bin/date '+%Y%m%d-%H%M%S')-$$"
if [[ -d "$sc_plugin" ]]; then
  sc_failed_active="$sc_backup_root/speech-capture.failed-$sc_timestamp"
  /bin/mv "$sc_plugin" "$sc_failed_active" || \
    sc_fail "无法把当前失败版本移入备份目录。"
fi
sc_restore_started=1

if [[ "$sc_smoke_test" == "1" && "${SPEECH_CAPTURE_RECOVERY_FAIL_AFTER_ACTIVE:-0}" == "1" ]]; then
  sc_fail "合成的当前版本迁移后故障。"
fi

if ! /bin/mv "$sc_backup" "$sc_plugin"; then
  sc_fail "无法原子恢复明确指定的旧版备份。"
fi

sc_restored_id="$(/usr/bin/plutil -extract id raw -o - "$sc_plugin/manifest.json" 2>/dev/null)"
sc_restored_version="$(/usr/bin/plutil -extract version raw -o - "$sc_plugin/manifest.json" 2>/dev/null)"
if [[ "$sc_restored_id" != "speech-capture" || "$sc_restored_version" != "$sc_backup_version" ||
      ! -f "$sc_plugin/main.js" || -L "$sc_plugin/main.js" ]]; then
  sc_fail "恢复后的插件身份、版本或主文件校验失败。"
fi

sc_restore_completed=1
echo
echo "Speech Capture 已恢复为 $sc_restored_version。"
echo "实际 Vault：$sc_vault"
echo "活动插件目录：$sc_plugin"
if [[ -n "$sc_failed_active" ]]; then
  echo "刚才的失败版本已保留在：$sc_failed_active"
fi
echo "现在可以重新打开 Obsidian，并确认设置页显示 $sc_restored_version。"
