from aiogram.fsm.state import State, StatesGroup


class OrderFlow(StatesGroup):
    choosing_service  = State()
    waiting_payment   = State()
    waiting_file      = State()
    awaiting_reupload = State()  # валидация не прошла, ждём новый файл
    processing        = State()


class AdminFlow(StatesGroup):
    waiting_price_key     = State()
    waiting_price_value   = State()
    waiting_turnitin_cred = State()
    waiting_receipt_del   = State()
    waiting_receipt_add   = State()
    waiting_admin_add     = State()
    waiting_admin_del     = State()
    waiting_kaspi_setting = State()  # link или expire_minutes
    waiting_free_users    = State()  # add или remove free user