# Shared, sourced only from the installed root-owned directory or installer tree.
FL_LANGUAGE=zh
load_language() {
    local saved=''
    if [[ -r "$CONFIG_DIR/language" ]]; then IFS= read -r saved < "$CONFIG_DIR/language" || :; fi
    case "$saved" in zh|en) FL_LANGUAGE=$saved;; esac
}
msg() { if [[ $FL_LANGUAGE == en ]]; then printf '%s' "$2"; else printf '%s' "$1"; fi; }
say() { msg "$@"; printf '\n'; }
choose_language() {
    local value=${1:-} tmp
    if [[ -z $value ]]; then
        printf '选择语言 / Choose language: 1) 中文 [zh]  2) English [en] [%s]: ' "$FL_LANGUAGE" >&2
        IFS= read -r value || return 1
        value=${value:-$FL_LANGUAGE}
    fi
    case "$value" in 1|zh) value=zh;; 2|en) value=en;; *) say '无效选择：zh 或 en' 'Invalid choice: zh or en' >&2; return 2;; esac
    if [[ ! -d $CONFIG_DIR ]]; then install -d -m 0750 "$CONFIG_DIR"; fi
    tmp=$(mktemp "$CONFIG_DIR/.language.XXXXXX")
    printf '%s\n' "$value" > "$tmp"
    mv -f -- "$tmp" "$CONFIG_DIR/language"
    FL_LANGUAGE=$value
    say '已切换到中文。' 'Switched to English.'
}
load_language
