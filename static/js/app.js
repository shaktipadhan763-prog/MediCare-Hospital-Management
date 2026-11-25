document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-confirm]");
  if (button && !window.confirm(button.dataset.confirm)) {
    event.preventDefault();
  }
});

const departmentSelect = document.getElementById("department_id");
const doctorSelect = document.getElementById("doctor_id");

if (departmentSelect && doctorSelect) {
  const filterDoctorsByDepartment = () => {
    for (const option of doctorSelect.options) {
      if (!option.value) {
        option.hidden = false;
        continue;
      }

      option.hidden = option.dataset.departmentId !== departmentSelect.value;
      if (option.selected && option.hidden) {
        doctorSelect.value = "";
      }
    }
  };

  departmentSelect.addEventListener("change", filterDoctorsByDepartment);
  filterDoctorsByDepartment();
}
