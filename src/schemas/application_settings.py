from pydantic import BaseModel


class ApplicationSettingBase(BaseModel):
    key: str
    value: str


class ApplicationSettingCreate(ApplicationSettingBase):
    pass


class ApplicationSettingUpdate(BaseModel):
    value: str


class ApplicationSettingRead(ApplicationSettingBase):
    id: int

    class Config:
        from_attributes = True