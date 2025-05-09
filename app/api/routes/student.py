from fastapi import APIRouter, Depends, Body, UploadFile, File
from fastapi.exceptions import HTTPException
from sqlalchemy import func
from app.models.instructor import Instructor
from app.models.lecture import Lecture
from app.models.video import Video
from app.models.enrollment import Enrollment
from app.models.watch_history import WatchHistory
from app.models.student import Student
from app.schemas.lecture import LectureListResponse
from app.schemas.student import (
    LectureVideoListRequest, LectureVideoListResponse,
    VideoLinkRequest, VideoLinkResponse,
    StudentProfileResponse,
    StudentNameUpdateRequest, StudentNameUpdateResponse,
    EnrollmentCancelRequest, EnrollmentCancelResponse, EnrollmentResponse, EnrollmentRequest,
    VideoProgressUpdateRequest, VideoProgressUpdateResponse
)
from app.services.auth_service import get_current_student
from app.services.student import (
    get_enrolled_lectures_for_student, get_lecture_videos_for_student, get_video_link_for_student, get_student_profile,
    update_student_name, cancel_enrollment, enroll_student_in_lecture, upload_profile_image_to_s3
)
from sqlalchemy.orm import Session
from app.dependencies.db import get_db
from app.dependencies.auth import get_current_student_uid

router = APIRouter(
    dependencies=[Depends(get_current_student)]
)

@router.get("/lecture", summary="내 수강신청 강의 목록")
def get_my_enrolled_lectures(
    db: Session = Depends(get_db),
    student_uid: str = Depends(get_current_student_uid)
):
    """
    학생이 본인 토큰으로 수강신청된 강의 목록을 조회합니다.
    - 강의 id, 강의 이름, instructor 이름 반환
    """
    lectures = get_enrolled_lectures_for_student(db, student_uid)
    return {"lectures": lectures}

@router.post("/lecture/video", response_model=LectureVideoListResponse, summary="특정 강의의 영상 목록 조회")
def get_lecture_video_list(
    req: LectureVideoListRequest = Body(...),
    db: Session = Depends(get_db),
    student_uid: str = Depends(get_current_student_uid)
):
    """
    특정 강의의 영상 목록을 조회합니다. (수강신청 여부 확인)
    - 미수강자는 403 에러 반환
    - 성공 시 video 리스트 반환
    """
    videos = get_lecture_videos_for_student(db, student_uid, req.lecture_id)
    return LectureVideoListResponse(videos=videos)

@router.post("/lecture/video/link", response_model=VideoLinkResponse, summary="특정 영상의 S3 링크 제공")
def get_video_s3_link(
    req: VideoLinkRequest = Body(...),
    db: Session = Depends(get_db),
    student_uid: str = Depends(get_current_student_uid)
):
    """
    학생이 video id를 제공하면, 수강신청 여부 확인 후 해당 영상의 S3 링크를 반환합니다.
    - 미수강자는 403 에러 반환
    - 영상이 없으면 404 에러 반환
    """
    return get_video_link_for_student(db, student_uid, req.video_id)

@router.get("/profile", response_model=StudentProfileResponse, summary="내 프로필 정보 조회")
def get_my_profile(
    db: Session = Depends(get_db),
    student_uid: str = Depends(get_current_student_uid)
):
    """
    학생이 본인 토큰으로 자신의 email, 이름을 조회합니다.
    """
    return get_student_profile(db, student_uid)

@router.patch("/profile/name", response_model=StudentNameUpdateResponse, summary="학생 이름 변경")
def set_my_name(
    req: StudentNameUpdateRequest = Body(...),
    db: Session = Depends(get_db),
    student_uid: str = Depends(get_current_student_uid)
):
    """
    학생이 본인 이름을 설정(변경)합니다.
    - 이름이 비어있거나 255자 초과면 400 반환
    - 학생 정보가 없으면 404 반환
    """
    return update_student_name(db, student_uid, req.name)

@router.post("/lecture/video/progress", response_model=VideoProgressUpdateResponse, summary="영상 진척도 기록")
def update_video_progress(
    req: VideoProgressUpdateRequest = Body(...),
    db: Session = Depends(get_db),
    student_uid: str = Depends(get_current_student_uid)
):
    # 1. 영상 조회
    video = db.query(Video).filter(Video.id == req.video_id, Video.is_public == 1).first()
    if not video:
        raise HTTPException(status_code=404, detail="해당 영상이 존재하지 않거나 비공개 상태입니다.")
    # 2. 수강신청 여부 확인
    enrollment = db.query(Enrollment).filter(
        Enrollment.student_uid == student_uid,
        Enrollment.lecture_id == video.lecture_id
    ).first()
    if not enrollment:
        raise HTTPException(status_code=403, detail="해당 강의에 수강신청되어 있지 않습니다.")
    # 3. 진척도 기록 (upsert)
    history = db.query(WatchHistory).filter(
        WatchHistory.student_uid == student_uid,
        WatchHistory.video_id == req.video_id
    ).first()
    if history:
        history.timestamp = func.now()
        history.watched_percent = max(getattr(history, 'watched_percent', 0), req.watched_percent)
    else:
        history = WatchHistory(
            student_uid=student_uid,
            video_id=req.video_id,
            timestamp=func.now(),
            watched_percent=req.watched_percent
        )
        db.add(history)
    db.commit()
    return VideoProgressUpdateResponse(message="진척도가 저장되었습니다.")

@router.post("/profile/image", summary="프로필 사진 업로드 및 저장")
def upload_my_profile_image(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    student_uid: str = Depends(get_current_student_uid)
):
    """
    학생이 본인 프로필 사진을 업로드하면 S3에 저장 후, 해당 URL을 DB에 저장합니다.
    """
    s3_url = upload_profile_image_to_s3(file)
    # DB에 저장
    student = db.query(Student).filter(Student.uid == student_uid).first()
    if not student:
        raise HTTPException(status_code=404, detail="학생 정보를 찾을 수 없습니다.")
    student.profile_image_url = s3_url
    db.commit()
    db.refresh(student)
    return {"profile_image_url": s3_url}

@router.get("/recent-incomplete-videos", summary="최근 시청기록 중 미완료 영상 10개 조회")
def get_recent_incomplete_videos(
    db: Session = Depends(get_db),
    student_uid: str = Depends(get_current_student_uid)
):
    """
    시청 완료하지 않은(95% 미만) 영상 중 최근 10개를 반환합니다.
    - video_id, lecture_id, lecture_name, video_name, instructor_name, timestamp 반환
    """
    results = (
        db.query(
            WatchHistory.video_id,
            Video.lecture_id,
            Lecture.name.label("lecture_name"),
            Video.title.label("video_name"),
            Instructor.name.label("instructor_name"),
            WatchHistory.timestamp,
            Video.video_image_url
        )
        .join(Video, WatchHistory.video_id == Video.id)
        .join(Lecture, Video.lecture_id == Lecture.id)
        .join(Instructor, Lecture.instructor_id == Instructor.id)
        .filter(WatchHistory.student_uid == student_uid)
        .filter(WatchHistory.watched_percent < 95)
        .order_by(WatchHistory.timestamp.desc())
        .limit(10)
        .all()
    )
    return [
        {
            "video_id": row.video_id,
            "lecture_id": row.lecture_id,
            "lecture_name": row.lecture_name,
            "video_name": row.video_name,
            "instructor_name": row.instructor_name,
            "timestamp": row.timestamp,
            "video_image_url": row.video_image_url
        }
        for row in results
    ]
