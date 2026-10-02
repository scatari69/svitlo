from aiogram.fsm.state import State, StatesGroup


class SubscriptionSetup(StatesGroup):
    region = State()
    group = State()
    name = State()
    channels = State()
    delete = State()
