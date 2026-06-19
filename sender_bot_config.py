"""Shared configuration for OTP sender scripts."""

OTP_TARGET_CHAT_ID = "-1003861839967"
LIVE_RELAY_BOT_TOKEN = "8725823333:AAHvjTvFDPPEkXZH2NDQj_E0FzA4OmXdTDA"
NUMBER_BOT_TOKEN = "8927209172:AAHzhWLI9jwMneO3g-c3RfuaP92uuIXX_Ws"
SENDER_BOT_TOKEN = LIVE_RELAY_BOT_TOKEN

SMS_PANEL = {
    "login_url": "http://teleroutex.com/auth/login",
    "otp_page_url": "https://teleroutex.com/Agent/Reports",
}

IVMS_PANEL = {
    "login_url": "https://www.ivasms.com/login",
    "active_sms_url": "https://www.ivasms.com/portal/live/my_sms",
}

VOICE_PANEL = {
    "base_domain": "https://www.orangecarrier.com",
    "login_url": "https://www.orangecarrier.com/login",
    "calls_url": "https://www.orangecarrier.com/live/calls",
}
