document.addEventListener('DOMContentLoaded', () => {
    // --- KHAI BÁO CÁC PHẦN TỬ GIAO DIỆN ---
    const plateOutInput = document.getElementById('plateOut');
    const plateInInput = document.getElementById('plateIn');
    const statusBadge = document.getElementById('statusBadge');
    
    // Hình ảnh khu vực Cắt biển (Dưới cùng - Cột phải)
    const imgCamOut = document.getElementById('imgCamOut'); 
    const imgCamIn = document.getElementById('imgCamIn');
    
    // Lưu ý: Chúng ta giữ khai báo 2 camera Live để code không lỗi, 
    // nhưng sẽ KHÔNG thay đổi .src của chúng trong quá trình chạy.
    const liveCamOut = document.getElementById('liveCamOut'); 
    const mainCamIn = document.getElementById('mainCamIn');   

    // Các trường văn bản thông tin
    const timeInDisplay = document.getElementById('timeInDisplay');
    const timeOutDisplay = document.getElementById('timeOutDisplay');
    const priceDisplay = document.getElementById('priceDisplay');
    const durationDisplay = document.getElementById('durationDisplay');
    const ticketCodeDisplay = document.getElementById('ticketCodeDisplay');

    /**
     * Hàm so sánh biển số lúc vào và lúc ra
     * Hiển thị màu sắc xanh/đỏ để cảnh báo bảo vệ
     */
    function comparePlates() {
        if (!plateOutInput || !plateInInput || !statusBadge) return;
        const valOut = plateOutInput.value.toUpperCase().trim();
        const valIn = plateInInput.value.toUpperCase().trim();

        if (valOut === "" || valIn === "") return;

        if (valOut !== valIn) {
            // Trạng thái KHÔNG KHỚP (Màu đỏ)
            plateOutInput.classList.remove('border-slate-700', 'focus:ring-blue-500', 'bg-slate-800');
            plateOutInput.classList.add('border-red-500', 'text-red-400', 'bg-red-900/20', 'focus:ring-red-400');
            statusBadge.innerHTML = 'BIỂN SỐ KHÔNG KHỚP';
            statusBadge.className = 'w-full bg-red-600/20 border border-red-500 text-red-400 font-bold px-4 py-3 rounded-lg text-lg tracking-wide mb-4 shadow-[0_0_15px_rgba(239,68,68,0.2)] transition-all';
        } else {
            // Trạng thái KHỚP (Màu xanh)
            plateOutInput.classList.add('border-slate-700', 'focus:ring-blue-500', 'bg-slate-800');
            plateOutInput.classList.remove('border-red-500', 'text-red-400', 'bg-red-900/20', 'focus:ring-red-400');
            statusBadge.innerHTML = 'HỢP LỆ - MỜI XE RA';
            statusBadge.className = 'w-full bg-emerald-600/20 border border-emerald-500 text-emerald-400 font-bold px-4 py-3 rounded-lg text-lg tracking-wide mb-4 shadow-[0_0_15px_rgba(16,185,129,0.2)] transition-all';
        }
    }

    /**
     * Hàm gọi API lưu biển số xuống Database
     */
    function savePlate(gate, newPlate) {
        const rfid = ticketCodeDisplay ? ticketCodeDisplay.innerText : "---";
        if (rfid === "---") return;
        
        fetch('/api/update_plate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rfid_code: rfid, gate: gate, new_plate: newPlate.toUpperCase().trim() })
        }).then(res => res.json())
          .then(data => {
              console.log('Update plate result:', data);
              if(data.status === "success") {
                  statusBadge.innerHTML = '✅ LƯU DATABASE THÀNH CÔNG!';
                  statusBadge.className = 'w-full bg-emerald-600/20 border border-emerald-500 text-emerald-400 font-bold px-4 py-3 rounded-lg text-lg tracking-wide mb-4 shadow-[0_0_15px_rgba(16,185,129,0.2)] transition-all';
              }
          })
          .catch(err => console.error('Error updating plate:', err));
    }

    // Lắng nghe nếu bảo vệ tự tay chỉnh sửa biển số
    if (plateOutInput) {
        plateOutInput.addEventListener('input', comparePlates);
        plateOutInput.addEventListener('change', () => savePlate('out', plateOutInput.value));
    }
    if (plateInInput) {
        plateInInput.addEventListener('input', comparePlates);
        plateInInput.addEventListener('change', () => savePlate('in', plateInInput.value));
    }

    // --- KẾT NỐI WEBSOCKET ---
    const ws = new WebSocket(`ws://${window.location.host}/ws`); 

    ws.onmessage = function(event) {
        const data = JSON.parse(event.data);
        console.log("Dữ liệu từ Server:", data);

        // Hiển thị mã thẻ RFID nếu có
        if (ticketCodeDisplay && data.rfid) {
            ticketCodeDisplay.innerText = data.rfid;
        }

        // ==================== XỬ LÝ XE VÀO ====================
        if (data.action === "IN") {
            // Chỉ cập nhật dữ liệu nếu đây là gói tin quét thẻ (có rfid)
            if (data.rfid !== undefined) {
                if(imgCamIn) imgCamIn.src = data.img_crop_in;         
                plateInInput.value = data.plate_in;
                
                // Reset khu vực Lối Ra để chờ
                if(imgCamOut) imgCamOut.src = "https://placehold.co/200x80/1a1a1a/475569?text=Waiting...";
                plateOutInput.value = "CHỜ XE RA...";
                
                if(timeInDisplay) timeInDisplay.innerText = data.time_in;
                if(timeOutDisplay) timeOutDisplay.innerText = "--:--:--";
                if(priceDisplay) priceDisplay.innerText = "-- đ";
                if(durationDisplay) durationDisplay.innerText = "Đang gửi...";
                
                // Reset màu sắc input
                plateOutInput.classList.remove('border-red-500', 'text-red-400', 'bg-red-900/20');
                plateOutInput.classList.add('border-slate-700', 'bg-slate-800');
            }

            if (data.warning) {
                statusBadge.innerHTML = data.warning;
                statusBadge.className = 'w-full bg-orange-600/20 border border-orange-500 text-orange-400 font-bold px-4 py-3 rounded-lg text-lg tracking-wide mb-4 shadow-[0_0_15px_rgba(249,115,22,0.2)]';
            } else {
                statusBadge.innerHTML = 'XE VÀO BÃI THÀNH CÔNG';
                statusBadge.className = 'w-full bg-blue-600/20 border border-blue-500 text-blue-400 font-bold px-4 py-3 rounded-lg text-lg tracking-wide mb-4 shadow-[0_0_15px_rgba(59,130,246,0.2)]';
            }

        // ==================== XỬ LÝ XE RA ====================
        } else if (data.action === "OUT") {
            // Chỉ cập nhật dữ liệu nếu đây là gói tin quét thẻ (có rfid)
            if (data.rfid !== undefined) {
                // Cập nhật các ảnh cắt biển (Crop) - TUYỆT ĐỐI KHÔNG ghi đè lên liveCamOut/mainCamIn
                if(imgCamOut) imgCamOut.src = data.img_crop_out;
                if(imgCamIn) imgCamIn.src = data.img_crop_in;     

                plateInInput.value = data.plate_in;
                plateOutInput.value = data.plate_out;

                if(timeInDisplay) timeInDisplay.innerText = data.time_in;
                if(timeOutDisplay) timeOutDisplay.innerText = data.time_out;
                if(durationDisplay) durationDisplay.innerText = data.duration || "--:--"; 
                if(priceDisplay) priceDisplay.innerText = "5,000 đ";
            }

            if (data.warning) {
                statusBadge.innerHTML = data.warning;
                statusBadge.className = 'w-full bg-orange-600/20 border border-orange-500 text-orange-400 font-bold px-4 py-3 rounded-lg text-lg tracking-wide mb-4 shadow-[0_0_15px_rgba(249,115,22,0.2)]';
            } else {
                comparePlates();
            }
        }
    };

    ws.onclose = () => {
        statusBadge.innerHTML = 'MẤT KẾT NỐI SERVER';
        statusBadge.className = 'w-full bg-red-900/40 border border-red-700 text-red-500 font-bold px-4 py-3 rounded-lg text-lg mb-4';
    };
});