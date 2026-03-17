"""신규도매 요청 라우터: SendGrid로 요청 메일 발송"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(tags=["신규도매 요청"])

logger = logging.getLogger(__name__)

RECIPIENT_EMAIL = "kjh@maipharm.com"
SENDER_EMAIL = "noreply@maipharm.com"


# ── Request/Response 스키마 ──


class SupplierRequestBody(BaseModel):
    supplier_name: str
    login_url: str
    login_id: str
    login_pw: str
    agreed: bool


class MessageResponse(BaseModel):
    success: bool
    message: str


# ── 엔드포인트 ──


@router.post("/supplier-request", response_model=MessageResponse)
def submit_supplier_request(req: SupplierRequestBody):
    """신규 도매상 크롤러 개발 요청 메일 전송."""
    if not req.agreed:
        raise HTTPException(
            status_code=400,
            detail="개인정보 제공에 동의해주세요.",
        )

    try:
        import sendgrid
        from sendgrid.helpers.mail import Mail, Email, To, Content

        sg = sendgrid.SendGridAPIClient()

        subject = f"[domae-mcp] 신규도매 크롤러 요청: {req.supplier_name}"
        body = (
            f"도매상명: {req.supplier_name}\n"
            f"로그인 URL: {req.login_url}\n"
            f"아이디: {req.login_id}\n"
            f"비밀번호: (보안을 위해 별도 전달)\n"
            f"개인정보 동의: {'동의' if req.agreed else '미동의'}\n"
        )

        message = Mail(
            from_email=Email(SENDER_EMAIL),
            to_emails=To(RECIPIENT_EMAIL),
            subject=subject,
            plain_text_content=Content("text/plain", body),
        )

        response = sg.send(message)

        if response.status_code in (200, 201, 202):
            return MessageResponse(success=True, message="요청이 전송되었습니다.")
        else:
            logger.error("SendGrid 응답 에러: %s", response.status_code)
            raise HTTPException(
                status_code=500,
                detail="메일 전송에 실패했습니다.",
            )

    except ImportError:
        logger.error("sendgrid 패키지가 설치되지 않았습니다.")
        raise HTTPException(
            status_code=500,
            detail="메일 전송 기능을 사용할 수 없습니다. (sendgrid 미설치)",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("메일 전송 실패: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="메일 전송 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
        )
