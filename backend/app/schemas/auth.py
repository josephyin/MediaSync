from pydantic import BaseModel, Field, model_validator


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


class AuthStatus(BaseModel):
    authenticated: bool
    username: str | None = None
    password_change_supported: bool = False


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=500)
    new_password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @model_validator(mode="after")
    def validate_passwords(self) -> "PasswordChangeRequest":
        if self.new_password.strip() == "":
            raise ValueError("新密码不能为空白字符串")
        if self.new_password == "admin":
            raise ValueError("新密码不能使用初始密码 admin")
        if self.new_password != self.confirm_password:
            raise ValueError("两次输入的新密码不一致")
        return self
