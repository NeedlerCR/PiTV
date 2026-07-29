#!/usr/bin/env bash
# /opt/pitv/otp-setup.sh
#
# Prints the TOTP secrets and otpauth:// URIs for all locked PiTV games.
# Add each URI to Google Authenticator, Authy, 1Password, or Bitwarden.
#
# To display a scannable QR code in the terminal:
#   sudo apt install qrencode
#   ./otp-setup.sh --qr

QR_MODE=false
[[ "${1:-}" == "--qr" ]] && QR_MODE=true

declare -A SECRETS=(
    ["SPACE INVADERS"]="JBSWY3DPEHPK3PXP"
    ["PAC-MAN"]="MFRA2YTMMFRA2YTM"
    ["SPACE SHOOTER"]="OB2XI2DFON2GK3TF"
    ["BOMBERMAN"]="KVKFKRCPNZQUYFY4"
    ["BREAKOUT"]="N5XCAZDGNZTGS3TN"
    ["VS TETRIS"]="NBQXG5DFMFZGKZLB"
)

declare -A KEYS=(
    ["SPACE INVADERS"]="INVADERS"
    ["PAC-MAN"]="PACMAN"
    ["SPACE SHOOTER"]="SHOOTER"
    ["BOMBERMAN"]="BOMBERMAN"
    ["BREAKOUT"]="BREAKOUT"
    ["VS TETRIS"]="TETRISDUEL"
)

echo ""
echo "=================================================================="
echo "  PiTV Game OTP Secrets"
echo "  Add each secret to your authenticator app."
echo "  Emergency bypass (always works): 159753"
echo "  Free games (no code needed): Snake, Tetris, Tic-Tac-Toe"
echo "=================================================================="
echo ""

for game in "SPACE INVADERS" "PAC-MAN" "SPACE SHOOTER" \
            "BOMBERMAN" "BREAKOUT" "VS TETRIS"; do
    secret="${SECRETS[$game]}"
    key="${KEYS[$game]}"
    label="PiTV:${key}"
    uri="otpauth://totp/${label}?secret=${secret}&issuer=PiTV&algorithm=SHA1&digits=6&period=30"

    echo "── ${game} ──"
    echo "   Secret : ${secret}"
    echo "   URI    : ${uri}"
    echo ""

    if $QR_MODE; then
        if command -v qrencode &>/dev/null; then
            echo "   QR code:"
            qrencode -t ANSI "$uri"
            echo ""
        else
            echo "   Install qrencode for QR codes:  sudo apt install qrencode"
            echo ""
        fi
    fi
done

echo "=================================================================="
echo "  How to add to authenticator app:"
echo "  1. Open Google Authenticator / Authy / 1Password"
echo "  2. Add account -> Enter setup key manually"
echo "  3. Paste the Secret above"
echo "  4. Set: Time-based, SHA1, 6 digits, 30s interval"
echo ""
echo "  Or run with --qr to display scannable QR codes:"
echo "  /opt/pitv/otp-setup.sh --qr"
echo "=================================================================="
