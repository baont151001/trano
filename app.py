import os
import math
import logging
from datetime import datetime, date, time, timedelta
import pytz
from sqlalchemy import (
    create_engine, Column, Integer, String, Date, Boolean, ForeignKey, DateTime
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Sessionfrom telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes, JobQueue
)

# =========================
# ENV / CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # BẮT BUỘC set trên Railway
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN as environment variable!")

PUBLIC_URL = os.getenv("PUBLIC_URL")  # BẮT BUỘC cho webhook, ví dụ: https://your-app.up.railway.app
if not PUBLIC_URL:
    raise RuntimeError("Please set PUBLIC_URL as environment variable!")

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook")  # bạn có thể đổi thành chuỗi bí mật
PORT = int(os.getenv("PORT", 8080))

TZ = os.getenv("TZ", "Asia/Ho_Chi_Minh")
LOCAL_TZ = pytz.timezone(TZ)

USER_MONTHLY_INCOME = int(os.getenv("USER_MONTHLY_INCOME", "8000000"))
USER_SIDE_INCOME_MONTHLY = int(os.getenv("USER_SIDE_INCOME_MONTHLY", "5000000"))
TOTAL_MONTHLY_INCOME = USER_MONTHLY_INCOME + USER_SIDE_INCOME_MONTHLY

# DB_URL mặc định trỏ vào volume /data (hãy mount volume trên Railway)
DB_URL = os.getenv("DB_URL", "sqlite:////data/debtbot.db")

# Ngày bắt đầu kế hoạch
START_PLAN = date.fromisoformat(os.getenv("START_PLAN", "2025-08-01"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Base = declarative_base()

# =========================
# MODELS
# =========================
class Loan(Base):
    __tablename__ = "loans"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    monthly_amount = Column(Integer, nullable=False)
    due_day = Column(Integer, nullable=False)          # 1..31
    months_left = Column(Integer, nullable=False)
    start_month = Column(Date, nullable=False)         # first month counted
    interest_only = Column(Boolean, default=False)
    is_borrow_no_interest = Column(Boolean, default=False)  # for 2.36M/2.8M/5M
    must_start_month = Column(Date, nullable=True)     # when borrow must begin to be paid
    pay_in_one_month = Column(Boolean, default=False)  # repay full in the starting month
    closed = Column(Boolean, default=False)

    # tracking
    last_paid_month = Column(Date, nullable=True)      # last "month" we considered paid

class SavingLog(Base):
    __tablename__ = "saving_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, default=0)
    date = Column(Date, nullable=False)
    amount = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class MonthlyPayment(Base):
    __tablename__ = "monthly_payments"

    id = Column(Integer, primary_key=True)
    loan_id = Column(Integer, ForeignKey("loans.id"), nullable=False)
    month = Column(Date, nullable=False)            # first day of month
    amount_required = Column(Integer, nullable=False)
    amount_paid = Column(Integer, default=0)
    is_paid = Column(Boolean, default=False)

    loan = relationship("Loan")

# =========================
# DB INIT
# =========================
engine = create_engine(DB_URL, echo=False, future=True)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

# =========================
# UTILITIES
# =========================
def first_day_of_month(d: date) -> date:
    return d.replace(day=1)

def last_day_of_month(d: date) -> date:
    next_month = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return next_month - timedelta(days=1)

def days_left_in_month(d: date) -> int:
    return (last_day_of_month(d) - d).days + 1

def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)

def ensure_default_plan(session: Session):
    """Create default loans if DB is empty."""
    count = session.query(Loan).count()
    if count > 0:
        return

    # Khoản vay
    session.add(Loan(
        name="Khoản #1",
        monthly_amount=2_000_000, due_day=17, months_left=3,
        start_month=START_PLAN, interest_only=False
    ))
    session.add(Loan(
        name="Khoản #2",
        monthly_amount=1_200_000, due_day=10, months_left=4,
        start_month=START_PLAN, interest_only=False
    ))
    session.add(Loan(
        name="Khoản #3",
        monthly_amount=2_650_000, due_day=10, months_left=9,
        start_month=START_PLAN, interest_only=False
    ))
    session.add(Loan(
        name="Khoản #4",
        monthly_amount=2_100_000, due_day=22, months_left=15,
        start_month=START_PLAN, interest_only=False
    ))
    session.add(Loan(
        name="Khoản #5 (lãi)",
        monthly_amount=700_000, due_day=10, months_left=15,
        start_month=START_PLAN, interest_only=True
    ))

    # Các khoản mượn
    session.add(Loan(
        name="Mượn 2.8M (trả trong T hiện tại)",
        monthly_amount=2_800_000, due_day=31, months_left=1,
        start_month=START_PLAN, is_borrow_no_interest=True, pay_in_one_month=True
    ))
    session.add(Loan(
        name="Mượn 2.36M (bắt đầu T10/2025)",
        monthly_amount=2_360_000, due_day=31, months_left=1,
        start_month=date(2025, 10, 1), is_borrow_no_interest=True,
        must_start_month=date(2025, 10, 1),
        pay_in_one_month=True
    ))
    session.add(Loan(
        name="Mượn 5M (bắt đầu T10/2025)",
        monthly_amount=5_000_000, due_day=31, months_left=1,
        start_month=date(2025, 10, 1), is_borrow_no_interest=True,
        must_start_month=date(2025, 10, 1),
        pay_in_one_month=True
    ))

    session.commit()

def get_month_pool_and_target(session: Session, today: date):
    month_start = first_day_of_month(today)

    loans = session.query(Loan).filter(Loan.closed == False).all()

    total_required_this_month = 0
    total_paid_this_month = 0

    for loan in loans:
        if loan.months_left <= 0:
            loan.closed = True
            continue

        if month_start < loan.start_month:
            continue
        if loan.must_start_month and month_start < loan.must_start_month:
            continue

        # Check or create MonthlyPayment
        mp = session.query(MonthlyPayment).filter_by(loan_id=loan.id, month=month_start).first()
        if not mp:
            mp = MonthlyPayment(
                loan_id=loan.id,
                month=month_start,
                amount_required=loan.monthly_amount,
                amount_paid=0,
                is_paid=False
            )
            session.add(mp)
            session.commit()

        total_required_this_month += mp.amount_required
        total_paid_this_month += mp.amount_paid

    remaining_this_month = max(0, total_required_this_month - total_paid_this_month)
    days_left = days_left_in_month(today)
    daily_target = math.ceil(remaining_this_month / days_left) if days_left > 0 else remaining_this_month

    return {
        "total_required": total_required_this_month,
        "total_paid": total_paid_this_month,
        "remaining": remaining_this_month,
        "days_left": days_left,
        "daily_target": daily_target
    }

def allocate_saving(session: Session, today: date, amount: int):
    """
    Ghi log & phân bổ tiền vào các MonthlyPayment chưa trả đủ trong tháng hiện tại.
    Ưu tiên theo due_day sớm.
    """
    log = SavingLog(date=today, amount=amount)
    session.add(log)
    session.commit()

    month_start = first_day_of_month(today)

    mps = (session.query(MonthlyPayment)
           .join(Loan, Loan.id == MonthlyPayment.loan_id)
           .filter(MonthlyPayment.month == month_start, MonthlyPayment.is_paid == False, Loan.closed == False)
           .order_by(Loan.due_day.asc())
           .all())

    remaining = amount
    for mp in mps:
        if remaining <= 0:
            break
        need = mp.amount_required - mp.amount_paid
        if need <= 0:
            mp.is_paid = True
            session.commit()
            continue

        pay = min(need, remaining)
        mp.amount_paid += pay
        remaining -= pay

        if mp.amount_paid >= mp.amount_required:
            mp.is_paid = True

        session.commit()

    close_loans_if_possible(session, today)
    return remaining

def close_loans_if_possible(session: Session, today: date):
    """
    Đóng các loan phù hợp (trả trong 1 tháng, hoặc khi months_left == 0).
    """
    month_start = first_day_of_month(today)
    loans = session.query(Loan).filter(Loan.closed == False).all()
    for loan in loans:
        mp = (session.query(MonthlyPayment)
              .filter_by(loan_id=loan.id, month=month_start)
              .first())

        # Trả 1 lần trong tháng và xong -> đóng ngay
        if loan.is_borrow_no_interest and loan.pay_in_one_month and mp and mp.is_paid:
            loan.months_left = 0
            loan.closed = True
            session.commit()

def decrease_months_left_for_last_month(session: Session, d: date):
    """
    Vào ngày 1 tháng mới, nếu tháng trước đã trả đủ, giảm months_left.
    """
    prev_month_start = first_day_of_month(d.replace(day=1) - timedelta(days=1))
    loans = session.query(Loan).filter(Loan.closed == False).all()
    for loan in loans:
        mp_prev = (session.query(MonthlyPayment)
                   .filter_by(loan_id=loan.id, month=prev_month_start)
                   .first())
        if mp_prev and mp_prev.is_paid and loan.months_left > 0 and not loan.is_borrow_no_interest:
            loan.months_left -= 1
            if loan.months_left <= 0:
                loan.closed = True
            session.commit()

# =========================
# HANDLERS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Khởi tạo DB mặc định
    with SessionLocal() as session:
        ensure_default_plan(session)

    text = (
        "Chào bạn! 👋\n\n"
        "Mình là bot theo dõi **tích lũy trả nợ**.\n\n"
        "Lệnh hữu ích:\n"
        "• /today – Mục tiêu tích luỹ hôm nay\n"
        "• /save <số_tiền> – Ghi số tiền bạn đã tích hôm nay (VD: /save 450000)\n"
        "• /status – Tiến độ trong tháng + các khoản đã/đang trả\n"
        "• /plan – Xem chi tiết các khoản vay còn lại\n"
        "• /help – Trợ giúp\n\n"
        "Mình sẽ tự động nhắc bạn lúc 05:00 mỗi ngày."
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Các lệnh:\n"
        "/today – Mục tiêu tích luỹ hôm nay\n"
        "/save <số_tiền> – Ghi số tiền đã tích hôm nay\n"
        "/status – Tiến độ tháng, còn bao nhiêu phải tích\n"
        "/plan – Xem các khoản vay\n"
    )
    await update.message.reply_text(text)

async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with SessionLocal() as session:
        loans = session.query(Loan).all()
        if not loans:
            await update.message.reply_text("Chưa có khoản vay nào.")
            return

        lines = []
        for l in loans:
            lines.append(
                f"{l.id}. {l.name} | {l.monthly_amount:,}đ/th | due {l.due_day} | "
                f"months_left={l.months_left} | closed={l.closed}"
            )
        await update.message.reply_text("\n".join(lines))

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_date = local_now().date()
    with SessionLocal() as session:
        ensure_default_plan(session)

        if today_date.day == 1:
            decrease_months_left_for_last_month(session, today_date)

        pool = get_month_pool_and_target(session, today_date)
        text = (
            f"Hôm nay {today_date.strftime('%d/%m/%Y')} cần tích lũy: "
            f"*{pool['daily_target']:,}đ*\n\n"
            f"Tháng này cần: {pool['total_required']:,}đ\n"
            f"Đã tích: {pool['total_paid']:,}đ\n"
            f"Còn lại: {pool['remaining']:,}đ\n"
            f"Số ngày còn lại trong tháng: {pool['days_left']}"
        )
        await update.message.reply_markdown(text)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today_date = local_now().date()
    with SessionLocal() as session:
        pool = get_month_pool_and_target(session, today_date)
        month_start = first_day_of_month(today_date)
        mps = (session.query(MonthlyPayment)
               .join(Loan, Loan.id == MonthlyPayment.loan_id)
               .filter(MonthlyPayment.month == month_start)
               .order_by(Loan.due_day.asc())
               .all())
        lines = []
        for mp in mps:
            l = mp.loan
            status_flag = "✅" if mp.is_paid else "❌"
            lines.append(
                f"{status_flag} {l.name} | cần {mp.amount_required:,}đ | đã trả {mp.amount_paid:,}đ | due {l.due_day}"
            )

        summary = (
            f"Tháng này cần: {pool['total_required']:,}đ\n"
            f"Đã tích: {pool['total_paid']:,}đ\n"
            f"Còn lại: {pool['remaining']:,}đ\n"
            f"Số ngày còn lại: {pool['days_left']}\n"
            f"Mục tiêu/ngày: {pool['daily_target']:,}đ\n\n"
            "Chi tiết các khoản:"
        )
        await update.message.reply_text(summary + "\n" + "\n".join(lines))

async def save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Dùng: /save <so_tien>. Ví dụ: /save 450000")
        return
    try:
        amt = int(context.args[0])
        if amt <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Số tiền không hợp lệ.")
        return

    today_date = local_now().date()
    with SessionLocal() as session:
        pool_before = get_month_pool_and_target(session, today_date)
        remaining_unalloc = allocate_saving(session, today_date, amt)
        pool_after = get_month_pool_and_target(session, today_date)

        achieved = (amt >= pool_before["daily_target"])
        status_text = "✅ ĐÃ ĐẠT mục tiêu ngày!" if achieved else "❌ CHƯA ĐẠT mục tiêu ngày."

        text = (
            f"{status_text}\n"
            f"Bạn vừa tích: {amt:,}đ (còn chưa phân bổ: {remaining_unalloc:,}đ)\n\n"
            f"Trước khi tích, mục tiêu/ngày là: {pool_before['daily_target']:,}đ\n"
            f"Giờ còn lại tháng này: {pool_after['remaining']:,}đ\n"
            f"Mục tiêu/ngày mới: {pool_after['daily_target']:,}đ "
            f"(còn {pool_after['days_left']} ngày)\n"
        )

        finished = (session.query(Loan)
                    .filter(Loan.closed == True)
                    .all())
        if finished:
            text += "\n---\nCác khoản đã tất toán:\n"
            for f in finished:
                text += f"• " + f.name + "\n"

        await update.message.reply_text(text)

async def daily_job(context):
    chat_id = context.job.chat_id
    today_date = local_now().date()
    with SessionLocal() as session:
        if today_date.day == 1:
            decrease_months_left_for_last_month(session, today_date)
        pool = get_month_pool_and_target(session, today_date)
        msg = (
            f"Hôm nay {today_date.strftime('%d/%m/%Y')} cần tích lũy: "
            f"*{pool['daily_target']:,}đ*\n"
            f"Còn lại tháng này: {pool['remaining']:,}đ – {pool['days_left']} ngày."
        )
    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")

async def start_and_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Đăng ký nhắc 05:00 hằng ngày cho chat hiện tại
    chat_id = update.effective_chat.id
    await start(update, context)

    # PTB v20: run_daily hỗ trợ tzinfo
    job_queue: JobQueue = context.application.job_queue
    job_queue.run_daily(
        daily_job,
        time=time(5, 0, tzinfo=LOCAL_TZ),
        chat_id=chat_id,
        name=f"daily_{chat_id}"
    )

    await update.message.reply_text("Đã đặt lịch nhắc lúc 05:00 mỗi ngày. Bạn sẽ nhận thông báo tích luỹ.")

def main():
    # Đảm bảo DB có dữ liệu mặc định
    with SessionLocal() as session:
        ensure_default_plan(session)

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    # Commands
    application.add_handler(CommandHandler("start", start_and_schedule))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("plan", plan))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("save", save))

    # Run webhook (không cần Flask)
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}",
    )

if __name__ == "__main__":
    main()
