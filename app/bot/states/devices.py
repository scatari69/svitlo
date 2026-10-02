from aiogram.fsm.state import State, StatesGroup


class DeviceSetup(StatesGroup):
    method = State()
    name = State()
    field = State()
    review = State()


class DeviceEdit(StatesGroup):
    name = State()


class DeviceDelete(StatesGroup):
    confirmation = State()
